"""External audit, 2026-10-21 (pipeline/coverage_critic.py, pipeline/release.py, pipeline/release_blockers.py).

A changed final is searched again (bounded); the calibration runs that PASSED a check are audited for a published
clause they break, by an executed demonstration; and an unsettled sentence a hidden check grades blocks on the
instruction. Every model call and every container is mocked. The two packages named by the audit --
a_f1_python-pdf-tooling and a_f0_school-scheduling-spa -- are the fixtures (fixtures/pass_audit: their task.toml,
instruction, checks, rubric, a suite file, the checks' rule_quote and real calibration grade records).
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from pipeline import coverage_critic as cc
from pipeline import policy, release, release_blockers, shipcheck
from pipeline.artifacts import atomic_json, identity
from pipeline.author import sandbox

FIXTURES = Path(__file__).parent / "fixtures/pass_audit"
PDF_STEP = "01-exam-dump-repair"
SCHOOL_STEP = "01-editor-every-class"
CARRIED_TEST = "test_examkit_edge.py::test_the_new_section_carries_on_from_the_last_question_of_the_paper"
DECOY_TEST = "test_examkit_edge.py::test_a_page_that_opens_on_a_number_that_is_not_a_question"


def _grade(name: str) -> dict:
    return json.loads((FIXTURES / "grades" / f"{name}.json").read_text())


def _quote(bundle: Path, cid: str) -> str:
    spec = json.loads((bundle / "authoring/spec.json").read_text())
    return next(c["rule_quote"][0] for c in spec["checks"] if c["id"] == cid)


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def _package(tmp: Path, name: str, repo: dict[str, str], at: str = "c-001-x") -> Path:
    bundle = Path(tmp) / at
    shutil.copytree(FIXTURES / name, bundle)
    # A shipped suite file is stored as <name>.fixture so pytest does not collect it from pipeline/tests.
    for stored in list(bundle.rglob("*.fixture")):
        stored.rename(stored.with_suffix(""))
    (bundle / "verification").mkdir(exist_ok=True)
    _write(bundle / "environment/repo", repo)
    return bundle


def _campaign(root: Path, model: str, step: str, trials: dict[str, tuple[dict, dict[str, str]]]) -> Path:
    """A calibration campaign as Harbor leaves it: report.json and, per trial, the step's grade record and tree."""
    path = Path(root) / f"calibration-{model.split('/')[-1]}"
    path.mkdir(parents=True)
    (path / "report.json").write_text(json.dumps({"requested_model": model}))
    for trial, (grade, files) in trials.items():
        folder = path / "jobs/run" / f"package__{trial}" / "steps" / step
        (folder / "verifier").mkdir(parents=True)
        (folder / "verifier/arena-grade.json").write_text(json.dumps(grade, ensure_ascii=False))
        _write(folder / "artifacts/app/repo", files)
    return path


def _index(bundle: Path, campaigns: list[Path]) -> None:
    atomic_json(bundle / "verification/native-calibrations.json",
                {"artifact_sha256": identity(bundle), "campaigns": [str(c) for c in campaigns], "topups": {}})


def _answer(doc: dict) -> str:
    return "Read both.\n```json\n" + json.dumps(doc, ensure_ascii=False) + "\n```"


class FakeSandbox:
    """The images, mocked. The reference passes every demonstration check (pa_<n>); a captured tree whose `rel` file
    holds `marker` fails them with the runner output `failure`."""

    def __init__(self, rel: str, marker: str, failure: str):
        self.rel, self.marker, self.failure = rel, marker, failure
        self.graded, self.checks_seen = [], []

    def patches(self):
        return [mock.patch.object(sandbox, "build_image", lambda b, role: f"{role}-tag"),
                mock.patch.object(sandbox, "solve_variant", lambda b, s: object()),
                mock.patch.object(sandbox, "verify", self.verify),
                mock.patch.object(sandbox, "grade_capture", self.grade_capture),
                mock.patch.object(cc, "reference_tree", lambda b, s, dest: dest)]

    @staticmethod
    def _demos(bundle) -> list[str]:
        return [c["id"] for c in json.loads((Path(bundle) / "tests/checks.json").read_text())["checks"]
                if c["id"].startswith("pa_")]

    def verify(self, bundle, variants, **_):
        doc = json.loads((Path(bundle) / "tests/checks.json").read_text())
        self.checks_seen.append(doc)
        rubric = json.loads((Path(bundle) / "tests/rubric.json").read_text())
        for cid in self._demos(bundle):
            assert any(cid in v for v in rubric.values()), "the demonstration check is not in the rubric"
        return {"steps": {s: {"valid": True, "reward": 1.0, "grade": {"checks": {
            cid: {"passed": True} for cid in self._demos(bundle)}}} for s in variants}}

    def grade_capture(self, bundle, step, capture, *, history=None, verifier_tag=None, work=None):
        self.graded.append(Path(capture).parent.parent.parent.name)
        bad = self.marker in (Path(capture) / "repo" / self.rel).read_text()
        return {"valid": True, "reward": 0.5 if bad else 1.0, "grade": {"checks": {
            cid: ({"passed": False, "gated_out": False, "exit_code": 1, "reason": "exit 1", "stdout": self.failure,
                   "stderr": ""} if bad else {"passed": True}) for cid in self._demos(bundle)}}}


def _settles_reference(expected: str, sentence: str):
    """A judge that picks the result carrying `expected` (the reference's) and names `sentence`."""
    def ask(model, prompt):
        first = prompt.split("RESULT X", 1)[1].split("RESULT Y", 1)[0]
        return f"The rule is plain.\nSETTLES: {'X' if expected in first else 'Y'}\nSENTENCE: {sentence}"
    return ask


# --------------------------------------------------------------------------- the fresh search of a changed final

class FreshSearchWhereTheTaskChanged(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lineage = self.tmp / "c-999-x" / "hardened"
        for n in (4, 6):
            _write(self.lineage / f"v{n}", {"steps/01-a/instruction.md": "keep my list as it is",
                                            "tests/suites/test_a.py": "def test_a():\n    pass\n"})
            (self.lineage / f"v{n}" / "verification").mkdir()
        for p in (mock.patch.object(cc, "_steps", lambda b: ["01-a"]),
                  mock.patch("pipeline.artifacts.identity", lambda b: f"sha-{Path(b).name}")):
            p.start()
            self.addCleanup(p.stop)
        row = {"index": 0, "clause": "keep my list", "violation": "adds a word", "applied": True, "gap": True,
               "edits": [{"path": "a.py", "old": "x", "new": "y"}]}
        (self.lineage / "v4" / cc.RECEIPT).write_text(json.dumps({
            "schema": cc.SCHEMA, "artifact_sha256": "sha-v4", "surface": cc.surface(self.lineage / "v4"),
            "steps": {"01-a": {"proposals": [row]}}, "gaps": [{"step": "01-a", **row}]}))

    def _ship(self, verified: dict, fresh: dict | None = None):
        v6 = self.lineage / "v6"
        judge = mock.Mock(return_value="clear\nPUBLISHED: YES\nREALISTIC: YES\nSENTENCE: keep my list as it is")
        with mock.patch.object(cc, "verify", return_value=verified) as v, \
                mock.patch.object(cc, "critic", return_value=fresh or {"steps": {}, "gaps": []}) as c, \
                mock.patch("pipeline.author.determinacy._ask", judge):
            out = cc.ship_critic(v6, record_dir=v6 / "verification")
        return out, v, c

    def test_an_unchanged_final_keeps_closed_and_verified_only(self):
        out, v, c = self._ship({"mode": "verify", "steps": {}, "gaps": []})
        c.assert_not_called()
        self.assertEqual(v.call_args.kwargs["record_dir"], self.lineage / "v6" / "verification")
        self.assertEqual(out["mode"], "verify")
        self.assertEqual(cc.changed_since(self.lineage / "v6", self.lineage / "v4",
                                          json.loads((self.lineage / "v4" / cc.RECEIPT).read_text())), {})

    def test_a_changed_instruction_triggers_a_fresh_bounded_search(self):
        (self.lineage / "v6/steps/01-a/instruction.md").write_text("keep my list as it is, and sort it")
        carried = [{"index": 0, "clause": "keep my list", "violation": "adds a word", "applied": True, "gap": False},
                   {"index": 4, "clause": "x", "violation": "y", "applied": True, "gap": False}]
        fresh_rows = [{"index": 0, "clause": "and sort it", "violation": "sorts descending", "applied": True,
                       "gap": True, "reward": 1.0, "edits": [{"path": "a.py", "old": "a", "new": "b"}]},
                      {"index": 1, "clause": "and sort it", "violation": "drops one", "applied": True, "gap": False}]
        out, v, c = self._ship({"mode": "verify", "steps": {"01-a": {"proposals": carried}}, "gaps": []},
                               {"model": "m", "steps": {"01-a": {"proposals": fresh_rows, "raw_chars": 40}},
                                "gaps": [{"step": "01-a", **fresh_rows[0]}]})
        self.assertIsNone(v.call_args.kwargs["record_dir"])         # one receipt, written after both parts
        self.assertEqual(c.call_args.kwargs["steps"], ["01-a"])
        self.assertEqual(c.call_args.kwargs["limit"], policy.CRITIC_FRESH_SEARCH_PROPOSALS)
        self.assertIs(c.call_args.kwargs["receipt"], False)
        self.assertEqual(out["mode"], "verify+search")
        self.assertEqual(out["fresh_search"]["changed"], {"01-a": ["instruction"]})
        rows = out["steps"]["01-a"]["proposals"]
        self.assertEqual([r["index"] for r in rows], [0, 4, 5, 6])  # fresh indices follow the carried ones
        self.assertEqual([g["index"] for g in out["gaps"]], [5])
        self.assertIs(out["gaps"][0]["blocking"], True)              # realistic and clearly published
        self.assertEqual([f["subject"] for f in cc.findings(out)], ["01-a#5"])
        written = json.loads((self.lineage / "v6" / cc.RECEIPT).read_text())
        self.assertEqual(written["mode"], "verify+search")
        self.assertEqual(len(written["steps"]["01-a"]["proposals"]), 4)
        # The merged receipt is a searching run: the next version answers to it.
        (self.lineage / "v7").mkdir()
        self.assertEqual(cc.source_record(self.lineage / "v7")[0].name, "v6")

    def test_suites_alone_do_not_search_again_an_instruction_does(self):
        # 2026-10-21: every repair adds hidden cases; re-searching each gap-closing round's result never converged.
        (self.lineage / "v6/tests/suites/test_b.py").write_text("def test_b():\n    pass\n")
        source = json.loads((self.lineage / "v4" / cc.RECEIPT).read_text())
        self.assertEqual(cc.changed_since(self.lineage / "v6", self.lineage / "v4", source), {})
        legacy = {k: v for k, v in source.items() if k != "surface"}
        self.assertEqual(cc.changed_since(self.lineage / "v6", self.lineage / "v4", legacy), {})  # read from v4 itself
        inst = self.lineage / "v6/steps/01-a/instruction.md"
        inst.write_text(inst.read_text() + "\nand one more rule.\n")
        self.assertEqual(cc.changed_since(self.lineage / "v6", self.lineage / "v4", source), {"01-a": ["instruction"]})
        self.assertEqual(cc.changed_since(self.lineage / "v6", self.lineage / "gone", legacy),
                         {"01-a": ["what the source run examined is not known"]})

    def test_the_search_is_bounded_and_kept_to_the_changed_steps(self):
        proposals = [{"clause": f"c{i}", "violation": f"v{i}", "edits": [{"path": "a.py", "old": "x = 1",
                                                                         "new": f"x = {i + 2}"}]} for i in range(5)]
        prompts, graded = [], []

        def ask(model, prompt):
            prompts.append(prompt)
            return _answer({"proposals": proposals})

        def reference_tree(bundle, step, dest):
            return _write(dest, {"a.py": "x = 1\n"})

        def verify(bundle, variants, **_):
            graded.append(variants["01-b"].files["a.py"])
            return {"steps": {"01-b": {"valid": True, "reward": 0.5, "grade": {"checks": {"o": {"passed": False}}}}}}

        record = self.tmp / "record"
        with mock.patch.object(cc, "_steps", lambda b: ["01-a", "01-b"]), \
                mock.patch.object(cc, "reference_tree", reference_tree), \
                mock.patch.object(cc, "build_prompt", lambda b, s, t, limit: f"up to {limit} for {s}"), \
                mock.patch.object(sandbox, "build_image", lambda b, kind: kind), \
                mock.patch.object(sandbox, "solve_variant", lambda b, s: s), \
                mock.patch.object(sandbox, "verify", verify):
            out = cc.critic(self.lineage / "v6", model="m", ask=ask, workers=1, record_dir=record,
                            steps=["01-b"], limit=2, receipt=False)
        self.assertEqual(list(out["steps"]), ["01-b"])
        self.assertEqual(prompts, ["up to 2 for 01-b"])
        self.assertEqual(sorted(graded), ["x = 2\n", "x = 3\n"])
        self.assertFalse((record / Path(cc.RECEIPT).name).exists())
        self.assertTrue((record / "coverage-critic-raw/01-b.txt").is_file())

    def test_a_carried_gap_that_was_not_re_executed_is_tried_again(self):
        source = {"steps": {"01-a": {"proposals": [
            {"index": 2, "clause": "c", "violation": "v", "applied": False, "gap": True,
             "edits": [{"path": "a.py", "old": "x = 1", "new": "x = 9"}]}]}}}
        with mock.patch.object(cc, "reference_tree", lambda b, s, dest: _write(dest, {"a.py": "x = 1\n"})), \
                mock.patch.object(sandbox, "build_image", lambda b, kind: kind), \
                mock.patch.object(sandbox, "solve_variant", lambda b, s: object()), \
                mock.patch.object(sandbox, "verify", return_value={"steps": {"01-a": {
                    "valid": True, "reward": 0.5, "grade": {"checks": {"o": {"passed": False}}}}}}):
            out = cc.verify(self.lineage / "v6", self.lineage / "v4", source, model="m", ask=mock.Mock(), workers=1)
        row = out["steps"]["01-a"]["proposals"][0]
        self.assertTrue(row["applied"])
        self.assertFalse(row["gap"])                                 # re-executed and closed, not dropped


# --------------------------------------------------------------------------- an unsettled sentence a check grades

class UnsettledButAsserted(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bundle = _package(self.tmp, "pdf_tooling", {"examkit/build.py": "start = last + 1\n"})
        self.scope = _quote(self.bundle, "o_labels_scope")

    def _gap(self, clause):
        return {"step": PDF_STEP, "index": 0, "clause": clause, "violation": "renumbers a sub-item list",
                "gap": True, "edits": [{"path": "examkit/build.py", "old": "a", "new": "b"}]}

    def _adjudicate(self, clause, replies):
        result = {"steps": {PDF_STEP: {"proposals": [self._gap(clause)]}}, "gaps": [self._gap(clause)]}

        def ask(model, prompt):
            reply = replies[model]
            if isinstance(reply, Exception):
                raise reply
            return reply
        return cc.adjudicate(self.bundle, result, ask=ask, workers=1, asserted=True)

    def test_an_unsettled_gap_on_a_graded_sentence_blocks_on_the_instruction(self):
        out = self._adjudicate(self.scope, {
            "claude-fable-5-1": f"two readings\nPUBLISHED: UNDETERMINED\nREALISTIC: YES\nSENTENCE: {self.scope}",
            "gpt-6-astra": "PUBLISHED: YES\nREALISTIC: YES"})
        gap = out["gaps"][0]
        self.assertIs(gap["blocking"], False)                        # no hidden case is owed for it
        self.assertIn("o_labels_scope", gap["asserted_by"])
        self.assertEqual(gap["route"], "instruction")
        self.assertEqual(gap["judgement"]["sentence"], self.scope)
        self.assertTrue(gap["judgement"]["instruction_sha256"])
        found = cc.findings(out)
        self.assertEqual([(f["check"], f["route"]) for f in found], [("coverage.unsettled", "instruction")])
        self.assertIn("State the rule in the user's voice", found[0]["detail"])
        self.assertEqual(cc.advisory(out), [])
        receipt = {"artifact_sha256": identity(self.bundle), **out}
        (self.bundle / cc.RECEIPT).write_text(json.dumps(receipt, ensure_ascii=False))
        with mock.patch("pipeline.review.review_findings", return_value=[]):
            blockers = release_blockers.collect(self.bundle)
        self.assertEqual([b["id"] for b in blockers], [f"UNSETTLED {PDF_STEP}#0"])
        self.assertTrue(release_blockers.needs_full_round(blockers))  # the agent sees the instruction

    def test_an_unsettled_gap_no_check_grades_stays_advisory(self):
        loose = "کد تو /app/repo هست."
        self.assertEqual(cc.asserting_checks(self.bundle, PDF_STEP, [loose]), [])
        out = self._adjudicate(loose, {
            "claude-fable-5-1": f"PUBLISHED: UNDETERMINED\nREALISTIC: YES\nSENTENCE: {loose}",
            "gpt-6-astra": "PUBLISHED: YES\nREALISTIC: YES"})
        self.assertIs(out["gaps"][0]["blocking"], False)
        self.assertNotIn("asserted_by", out["gaps"][0])
        self.assertEqual(cc.findings(out), [])
        self.assertEqual([f["check"] for f in cc.advisory(out)], ["coverage.gap_advisory"])

    def test_an_erring_judge_never_demotes(self):
        out = self._adjudicate(self.scope, {"claude-fable-5-1": RuntimeError("401"),
                                            "gpt-6-astra": TimeoutError("read timed out")})
        self.assertIs(out["gaps"][0]["blocking"], True)
        self.assertEqual([f["check"] for f in cc.findings(out)], ["coverage.gap"])

    def test_a_judgement_holds_only_while_its_instruction_does(self):
        asked = []

        def ask(model, prompt):
            asked.append(model)
            return "PUBLISHED: YES\nREALISTIC: YES"
        digest = cc._digest(self.bundle / "steps" / PDF_STEP / "instruction.md")
        cases = [({"blocking": False, "why": "every judge calls it contrived, not a realistic mistake"}, False),
                 ({"blocking": False, "unsettled": True, "why": "x", "instruction_sha256": digest}, False),
                 ({"blocking": False, "unsettled": True, "why": "x", "instruction_sha256": "older wording"}, True),
                 ({"blocking": False, "why": "the instruction does not clearly settle it (gpt-6-astra)"}, True)]
        for judgement, rejudged in cases:
            with self.subTest(judgement=judgement):
                asked.clear()
                result = {"steps": {}, "gaps": [self._gap(self.scope)]}
                cc.adjudicate(self.bundle, result, prior={"gaps": [dict(self._gap(self.scope), judgement=judgement)]},
                              ask=ask, workers=1, asserted=True)
                self.assertEqual(bool(asked), rejudged)


# --------------------------------------------------------------------------- the pass audit

class PassAuditPdfTooling(unittest.TestCase):
    """8 of 10 Astra runs renumbered the carried paper 51..54 as 1..4 and passed the test that should have caught it
    (it reads the expected number from the solver's own output) -- inside o_labels_digits, which they failed on its
    decoy test, so the credit is test-level."""

    EXPECTED = "the paper keeps ۵۱- to ۵۴-; history continues at ۵۵- and ۵۶-"
    OBSERVED = "the paper becomes ۱- to ۴-; history continues at ۵- and ۶-"
    FAILURE = ("F                                                                        [100%]\n"
               "=================================== FAILURES ===================================\n"
               "____________________ test_the_carried_paper_keeps_its_numbers ____________________\n\n"
               "    def test_the_carried_paper_keeps_its_numbers():\n"
               ">       assert got == want, first_difference(got, want)\n"
               "E       AssertionError: line 0: '۱-سؤال' != '۵۱-سؤال'\n\n"
               "test_pass_audit_1.py:14: AssertionError\n"
               "=========================== short test summary info ============================\n"
               "FAILED test_pass_audit_1.py::test_the_carried_paper_keeps_its_numbers\n1 failed in 0.21s\n")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bundle = _package(self.tmp, "pdf_tooling", {"examkit/build.py": "start = last_of_paper + 1\n"})
        self.carried = _quote(self.bundle, "o_start_number")
        astra = _grade("pdf_tooling-astra-29DWMD2")
        fable = json.loads(json.dumps(astra))
        for record in fable["checks"].values():
            record.update(passed=True, stdout="", stderr="")
        native = self.tmp / "pipeline-artifacts/native/aa"
        self.campaigns = [
            _campaign(native, "gpt-6-astra", PDF_STEP, {
                "29DWMD2": (astra, {"examkit/build.py": "start = 1  # renumber the paper\n"}),
                "jdrJv8s": (astra, {"examkit/build.py": "start = 1  # renumber the paper\n"}),
                "bZxU453": (astra, {"examkit/build.py": "start = 1  # renumber the paper\nreset = True\n"})}),
            _campaign(native, "claude-fable-5-1", PDF_STEP, {
                "ZTQtjiu": (fable, {"examkit/build.py": "start = last_of_paper + 1\nkept = True\n"})})]
        _index(self.bundle, self.campaigns)
        self.prompts = []

    def critic_ask(self, model, prompt):
        self.prompts.append(prompt)
        if "start = 1" not in prompt.split("THE SOLUTION'S CHANGE", 1)[1]:
            return _answer({"demonstrations": []})
        return _answer({"demonstrations": [{
            "check": "o_labels_digits", "clause": self.carried, "violation": "renumbers the paper from 1",
            "input": "a paper whose questions run 51 to 54 over two pages, two history questions appended",
            "expected": self.EXPECTED, "observed": self.OBSERVED, "file": "test_pass_audit_1.py",
            "select": ["test_pass_audit_1.py::test_the_carried_paper_keeps_its_numbers"],
            "content": "from test_examkit_edge import built, carried_pages, doc_of\n\n"
                       "def test_the_carried_paper_keeps_its_numbers():\n    ...\n"}]})

    def _audit(self, judge, **kw):
        fake = FakeSandbox("examkit/build.py", "start = 1", self.FAILURE)
        with ExitStack() as stack:
            for p in fake.patches():
                stack.enter_context(p)
            record = cc.pass_audit(self.bundle, model="critic", ask=self.critic_ask, judge=judge, workers=1,
                                   record_dir=self.bundle / "verification", **kw)
        return record, fake

    def test_a_demonstrated_violation_by_a_passing_run_blocks_and_is_recorded(self):
        record, fake = self._audit(_settles_reference(self.EXPECTED, self.carried))
        self.assertTrue(record["complete"], record["incomplete"])
        self.assertEqual(record["artifact_sha256"], identity(self.bundle))
        self.assertTrue(record["grading_sha256"])
        entry = record["steps"][PDF_STEP]
        same = next(s for s in entry["solutions"] if s["trial"] == "package__29DWMD2")
        self.assertEqual([r["trial"] for r in same["runs"]], ["package__29DWMD2", "package__jdrJv8s"])  # deduped
        self.assertIn(CARRIED_TEST, same["partial"]["o_labels_digits"])
        self.assertNotIn(DECOY_TEST, same["partial"]["o_labels_digits"])
        self.assertEqual(sum(1 for s in entry["solutions"] if s["audited"]), 3)
        astra_prompt = next(p for p in self.prompts if "renumber the paper" in p)
        self.assertIn("o_labels_digits | objective | PASSED these of its tests", astra_prompt)
        self.assertIn(CARRIED_TEST, astra_prompt)
        # Two Astra demonstrations chose one file name: each kept a file of its own.
        files = sorted(d["file"] for d in entry["demonstrations"])
        self.assertEqual(files, ["test_pass_audit_1.py", "test_pass_audit_1_2.py"])
        demo_checks = [c for c in fake.checks_seen[0]["checks"] if c["id"].startswith("pa_")]
        self.assertEqual([c["runner"]["select"] for c in demo_checks],
                         [["test_pass_audit_1.py::test_the_carried_paper_keeps_its_numbers"],
                          ["test_pass_audit_1_2.py::test_the_carried_paper_keeps_its_numbers"]])
        gaps = record["gaps"]
        self.assertEqual([(g["id"], g["route"], g["blocking"]) for g in gaps],
                         [("p1", "hidden", True), ("p2", "hidden", True)])
        self.assertEqual(sorted(r["trial"] for r in gaps[0]["violators"]),
                         ["package__29DWMD2", "package__bZxU453", "package__jdrJv8s"])
        self.assertEqual(gaps[0]["failure"]["kind"], "assertion")
        self.assertEqual(gaps[0]["judgement"]["realistic"], "by construction: a calibration solution")
        patch = Path(gaps[0]["patch"]).read_text()
        self.assertIn("-start = last_of_paper + 1", patch)
        self.assertIn("+start = 1  # renumber the paper", patch)
        written = json.loads((self.bundle / cc.PASS_RECEIPT).read_text())
        self.assertEqual(len(written["gaps"]), 2)
        found = cc.pass_findings(written)
        self.assertEqual([f["check"] for f in found], ["pass.gap", "pass.gap"])
        self.assertIn("(+2 more) of o_labels_digits while violating", found[0]["detail"])   # test-level credit
        self.assertIn("3 calibration run(s) in all", found[0]["detail"])
        # Release reads it: a blocking gap on the calibration of record.
        problems = cc.pass_problems(self.bundle)
        self.assertEqual(len(problems), 2)
        self.assertTrue(problems[0].startswith(f"{PDF_STEP}#p1: calibration run gpt-6-astra package__29DWMD2"))
        # The release-repair round gets it as its own blocker class, with the executed demonstration.
        with mock.patch.object(shipcheck, "coverage", return_value=[]), \
                mock.patch("pipeline.review.review_findings", return_value=[]):
            blockers = release_blockers.collect(self.bundle)
        self.assertEqual([b["id"] for b in blockers], [f"PASS {PDF_STEP}#p1", f"PASS {PDF_STEP}#p2"])
        self.assertIn("test_the_carried_paper_keeps_its_numbers", blockers[0]["evidence"])
        self.assertIn("+start = 1", blockers[0]["evidence"])
        self.assertFalse(release_blockers.needs_full_round(blockers))       # hidden-only
        self.assertIn("2b. PASS", release_blockers.brief(blockers, self.bundle))
        (self.bundle / release_blockers.CONTEXT).write_text(json.dumps({"critic_source": None, "blockers": blockers}))
        spec = {"steps": [], "checks": [], "suites": {}}
        self.assertEqual(len(release_blockers.requirement(spec, self.bundle)), 2)
        notes = "\n".join(f"{b['id']}: FIXED -- carried_paper case: before 1.0, after 0.0" for b in blockers)
        self.assertEqual(release_blockers.requirement(dict(spec, solution_notes=notes), self.bundle), [])

    def test_an_unsettled_demonstration_on_a_graded_sentence_routes_to_the_instruction(self):
        scope = _quote(self.bundle, "o_labels_scope")

        def judge(model, prompt):
            if model == "claude-fable-5-1":
                return f"It reads two ways.\nSETTLES: UNDETERMINED\nSENTENCE: «{scope}»"
            return _settles_reference(self.EXPECTED, self.carried)(model, prompt)
        record, _ = self._audit(judge)
        gap = record["gaps"][0]
        self.assertEqual(gap["route"], "instruction")
        self.assertIn("o_labels_scope", gap["asserted_by"])
        self.assertEqual(gap["judgement"]["sentence"], f"«{scope}»")
        self.assertEqual({f["check"] for f in cc.pass_findings(record)}, {"pass.unsettled"})
        with mock.patch.object(shipcheck, "coverage", return_value=[]), \
                mock.patch("pipeline.review.review_findings", return_value=[]):
            blockers = release_blockers.collect(self.bundle)
        self.assertEqual(blockers[0]["id"], f"UNSETTLED {PDF_STEP}#p1")
        self.assertTrue(release_blockers.needs_full_round(blockers))

    def test_an_unsettled_demonstration_no_check_grades_is_advisory_with_its_sentence(self):
        loose = "کد تو /app/repo هست."
        self.carried = loose                                     # the demonstration quotes an ungraded sentence
        self.assertEqual(cc.asserting_checks(self.bundle, PDF_STEP, [loose]), [])
        record, _ = self._audit(lambda m, p: f"SETTLES: UNDETERMINED\nSENTENCE: {loose}")
        self.assertEqual(record["gaps"], [])
        self.assertEqual([g["route"] for g in record["advisory"]], ["none", "none"])
        self.assertEqual(cc.pass_problems(self.bundle), [])
        advisory = cc.pass_advisory(record)
        self.assertIn(loose, advisory[0]["detail"])

    def test_an_erring_judge_never_passes(self):
        def erring(model, prompt):
            raise RuntimeError("HTTP 529 overloaded")
        record, _ = self._audit(erring)
        self.assertEqual([g["route"] for g in record["gaps"]], ["hidden", "hidden"])
        # A doubt that quotes no sentence of the instruction is not a demotion either.
        record, _ = self._audit(lambda m, p: "SETTLES: UNDETERMINED\nSENTENCE: the user never said")
        self.assertEqual([g["blocking"] for g in record["gaps"]], [True, True])
        self.assertIn("without quoting the deciding sentence", record["gaps"][0]["judgement"]["why"])

    def test_an_audit_that_could_not_run_is_unverified_never_clean(self):
        def broken(model, prompt):
            raise ConnectionError("reset by peer")
        fake = FakeSandbox("examkit/build.py", "start = 1", self.FAILURE)
        with ExitStack() as stack:
            for p in fake.patches():
                stack.enter_context(p)
            record = cc.pass_audit(self.bundle, model="critic", ask=broken, judge=mock.Mock(), workers=1,
                                   record_dir=self.bundle / "verification")
        self.assertFalse(record["complete"])
        self.assertTrue(any("not audited (ConnectionError" in i for i in record["incomplete"]))
        self.assertTrue(cc.pass_problems(self.bundle)[0].startswith("unverified, run it again"))
        with ExitStack() as stack:
            for p in fake.patches():
                stack.enter_context(p)
            stack.enter_context(mock.patch.object(sandbox, "verify", side_effect=RuntimeError("docker build failed")))
            record = cc.pass_audit(self.bundle, model="critic", ask=self.critic_ask, judge=mock.Mock(), workers=1)
        self.assertFalse(record["complete"])
        self.assertTrue(any("could not be graded on the reference" in i for i in record["incomplete"]))
        missing = [{"campaign": "/gone/calibration-x", "path": "/gone/calibration-x", "model": "m"}]
        record = cc.pass_audit(self.bundle, model="critic", ask=self.critic_ask, campaigns=missing)
        self.assertFalse(record["complete"])
        (self.bundle / cc.PASS_RECEIPT).unlink()
        self.assertEqual(cc.pass_problems(self.bundle),
                         ["the pass audit has not run on this version's calibration of record"])


class PassAuditSchoolScheduling(unittest.TestCase):
    """Fable runs jRDmqrK, k9P57Dq and oem2t87 merged 刘䶰 with 刘䶮 (a CJK Extension-A character outside the BMP range
    their regex covers) and got full o_identity credit. node-test keeps no output, so the failure is read again."""

    FIXTURE = "const m = name.match(/^[\\u4e00-\\u9fff]+/)\n"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bundle = _package(self.tmp, "school_scheduling", {"engine.js": self.FIXTURE})
        self.clause = _quote(self.bundle, "o_identity")
        merged, refused = _grade("school_scheduling-fable-jRDmqrK"), _grade("school_scheduling-fable-FM3dDJS")
        self.assertTrue(merged["checks"]["o_identity"]["passed"])
        self.assertFalse(refused["checks"]["o_identity"]["passed"])
        native = self.tmp / "pipeline-artifacts/native/bb"
        self.campaigns = [_campaign(native, "claude-fable-5-1", SCHOOL_STEP, {
            "jRDmqrK": (merged, {"engine.js": self.FIXTURE + "// canonTeacher, keyed\n"}),
            "k9P57Dq": (merged, {"engine.js": self.FIXTURE + "// canonTeacher, both sides\n"}),
            "oem2t87": (merged, {"engine.js": self.FIXTURE + "// canonTeacher, trimmed\n"}),
            "FM3dDJS": (refused, {"engine.js": "const m = name.match(/^[\\u3400-\\u9fff]+/)\n"})})]
        _index(self.bundle, self.campaigns)

    def critic_ask(self, model, prompt):
        if "o_identity | objective | PASSED" not in prompt:
            return _answer({"demonstrations": []})
        return _answer({"demonstrations": [{
            "check": "o_identity", "clause": self.clause, "violation": "reads only the BMP Han block",
            "input": "刘䶰 Liu Yan typed for H-A2 Friday P2, where 刘䶮 teaches",
            "expected": "accepted: 刘䶰 is another teacher", "observed": "refused: taken for 刘䶮",
            "file": "test_pass_audit_identity.js", "select": ["test_pass_audit_identity.js"],
            "content": "const test = require('node:test');\nconst assert = require('node:assert');\n"
                       "test('an Extension-A sibling is another teacher', () => { assert.ok(true); });\n"}]})

    def _audit(self):
        fake = FakeSandbox("engine.js", "\\u4e00-\\u9fff", "")
        explained = ("not ok 1 - an Extension-A sibling is another teacher\n"
                     "  AssertionError [ERR_ASSERTION]: 刘䶰 Liu Yan was refused at H-A2 Friday P2\n"
                     "      at TestContext.<anonymous> (/tests/suites/test_pass_audit_identity.js:14:12)\n")
        with ExitStack() as stack:
            for p in fake.patches():
                stack.enter_context(p)
            stack.enter_context(mock.patch("pipeline.genuine.explain", return_value=explained))
            return cc.pass_audit(self.bundle, model="critic", ask=self.critic_ask, workers=1,
                                 judge=_settles_reference("accepted", self.clause),
                                 record_dir=self.bundle / "verification")

    def test_every_merging_run_is_caught(self):
        record = self._audit()
        self.assertTrue(record["complete"], record["incomplete"])
        self.assertEqual(sorted(g["trial"] for g in record["gaps"]),
                         ["package__jRDmqrK", "package__k9P57Dq", "package__oem2t87"])
        for gap in record["gaps"]:
            self.assertEqual(gap["failure"]["kind"], "assertion")     # read again: node-test kept no output
            self.assertEqual(sorted(r["trial"] for r in gap["violators"]),
                             ["package__jRDmqrK", "package__k9P57Dq", "package__oem2t87"])

    def test_the_cap_bounds_the_reading_not_the_execution(self):
        with mock.patch.object(policy, "PASS_AUDIT_SOLUTIONS_PER_CHECK", 1):
            record = self._audit()
        audited = [s for s in record["steps"][SCHOOL_STEP]["solutions"] if s["audited"]]
        self.assertEqual(sum(1 for s in audited if "o_identity" in s["passed"]), 1)
        self.assertEqual(len(record["gaps"]), 1)
        self.assertEqual(len(record["gaps"][0]["violators"]), 3)      # every passing run was executed


class Selection(unittest.TestCase):
    """Real school-scheduling campaigns, 2026-10-21: with one cap for both families every o_identity pick was taken
    before a Fable run came up, and the three Fable runs that merged two teachers were never read."""

    @staticmethod
    def _solution(key, model, credited, passed_units=()):
        return {"key": key, "model": model, "trial": f"package__{key}", "credited": credited,
                "credit_before": {"passed": [list(u) for u in passed_units], "failed": []}}

    def test_each_family_is_read_for_every_check(self):
        pool = [self._solution("a1", "gpt-6-astra", ["o_bad_date", "o_identity"]),
                self._solution("a2", "gpt-6-astra", ["o_bad_date", "o_identity"]),
                self._solution("f1", "claude-fable-5-1", ["o_identity"]),
                self._solution("f2", "claude-fable-5-1", ["o_bad_date"])]
        chosen = cc.select_solutions(pool, 1, {"o_bad_date", "o_identity"})
        self.assertEqual(sorted(s["key"] for s in chosen), ["a1", "f1", "f2"])

    def test_within_a_family_a_different_credit_comes_first(self):
        same, other = [("o_x", "*"), ("o_y", "*")], [("o_x", "*")]
        pool = [self._solution("a1", "gpt-6-astra", ["o_x"], same), self._solution("a2", "gpt-6-astra", ["o_x"], same),
                self._solution("a3", "gpt-6-astra", ["o_x"], other)]
        self.assertEqual([s["key"] for s in cc.select_solutions(pool, 2, {"o_x"})], ["a1", "a3"])


class CarriedGaps(unittest.TestCase):
    """The lineage's previous audit answers to the next version, closed-and-verified."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.v3 = _package(self.tmp, "pdf_tooling", {"examkit/build.py": "start = last + 1\n"}, at="c-1/hardened/v3")
        self.v4 = _package(self.tmp, "pdf_tooling", {"examkit/build.py": "start = last + 1\n"}, at="c-1/hardened/v4")
        capture = _write(self.tmp / "run/steps" / PDF_STEP / "artifacts/app", {"repo/examkit/build.py": "start = 1\n"})
        digest = cc._digest(self.v4 / "steps" / PDF_STEP / "instruction.md")
        gap = {"step": PDF_STEP, "id": "p1", "check": "o_labels_digits", "route": "hidden", "blocking": True,
               "clause": "c", "input": "i", "expected": "e", "observed": "o", "capture": str(capture), "history": {},
               "credit_before": {"passed": [["o_labels_digits", CARRIED_TEST]],
                                 "failed": [["o_labels_digits", "*"], ["o_labels_digits", DECOY_TEST]]},
               "judgement": {"unsettled": False, "instruction_sha256": digest}}
        atomic_json(self.v3 / cc.PASS_RECEIPT, {"schema": cc.PASS_SCHEMA, "gaps": [gap]})
        self.capture = capture

    def _recheck(self, failing: list[str]):
        stdout = "".join(f"FAILED {t}\n" for t in failing)
        outcome = {"valid": True, "reward": 0.5, "grade": {"checks": {
            "o_labels_digits": {"passed": False, "exit_code": 1, "stdout": stdout, "stderr": ""}}}}
        with mock.patch.object(sandbox, "build_image", lambda b, role: role), \
                mock.patch.object(sandbox, "grade_capture", return_value=outcome):
            return cc.recheck_carried(self.v4, *cc._earlier_pass_record(self.v4), judge=mock.Mock(), workers=1)

    def test_a_gap_closes_only_when_the_run_loses_credit_it_had(self):
        still, demoted, closed = self._recheck([DECOY_TEST, CARRIED_TEST])
        self.assertEqual((len(still), len(demoted)), (0, 0))
        self.assertEqual(closed[0]["recheck"]["newly_failed"], [["o_labels_digits", CARRIED_TEST]])
        still, _, closed = self._recheck([DECOY_TEST])
        self.assertEqual(closed, [])
        self.assertEqual((still[0]["id"], still[0]["carried_from"]), ("v3.p1", "v3"))
        self.assertIn("still earns every check it passed on v3", cc.pass_findings({"gaps": still})[0]["detail"])

    def test_a_run_that_cannot_be_re_graded_stays_open(self):
        shutil.rmtree(self.capture / "repo")
        still, _, closed = self._recheck([CARRIED_TEST])
        self.assertEqual(closed, [])
        self.assertIn("the captured run is gone", still[0]["recheck"]["error"])

    def test_the_audit_of_the_next_version_carries_them(self):
        with mock.patch.object(sandbox, "build_image", lambda b, role: role), \
                mock.patch.object(sandbox, "grade_capture", return_value={"valid": True, "grade": {"checks": {}}}):
            record = cc.pass_audit(self.v4, model="critic", ask=mock.Mock(), campaigns=[])
        self.assertEqual([g["id"] for g in record["gaps"]], ["v3.p1"])
        self.assertEqual(record["carried_from"], str(self.v3.resolve()))   # macOS: /var is a symlink
        self.assertFalse(record["complete"])                          # and nothing of its own was audited


# --------------------------------------------------------------------------- release wiring

class ReleaseWiring(unittest.TestCase):
    def setUp(self):
        from pipeline.tests.test_production import synthetic_bundle
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bundle = synthetic_bundle(self.tmp.resolve())
        self.sha = identity(self.bundle)
        _index(self.bundle, [Path("/c/0"), Path("/c/1")])

    def test_qualify_reuses_a_current_audit_and_records_one_that_could_not_run(self):
        current = {"schema": cc.PASS_SCHEMA, "artifact_sha256": self.sha, "campaigns": ["/c/0", "/c/1"],
                   "complete": True, "gaps": []}
        atomic_json(self.bundle / cc.PASS_RECEIPT, current)
        with mock.patch.object(cc, "pass_audit") as audit:
            self.assertEqual(release._pass_audit(self.bundle, self.sha)["campaigns"], ["/c/0", "/c/1"])
            audit.assert_not_called()
        atomic_json(self.bundle / cc.PASS_RECEIPT, dict(current, campaigns=["/c/0"]))
        with mock.patch.object(cc, "pass_audit", side_effect=RuntimeError("no docker")) as audit:
            record = release._pass_audit(self.bundle, self.sha)
            audit.assert_called_once()
        self.assertFalse(record["complete"])
        written = json.loads((self.bundle / cc.PASS_RECEIPT).read_text())
        self.assertIn("no docker", written["incomplete"][0])
        self.assertTrue(cc.pass_problems(self.bundle, self.sha)[0].startswith("unverified"))

    def test_evaluate_blocks_on_an_open_gap_and_on_a_missing_audit(self):
        errors = [e for e in release._evaluate(self.bundle)["errors"] if e.startswith("pass audit:")]
        self.assertEqual(errors, ["pass audit: the pass audit has not run on this version's calibration of record"])
        gap = {"step": "01-a", "id": "p1", "route": "hidden", "model": "gpt-6-astra", "trial": "package__x",
               "check": "o_x", "clause": "keep it", "input": "i", "expected": "e", "observed": "o", "content": "t"}
        atomic_json(self.bundle / cc.PASS_RECEIPT, {"schema": cc.PASS_SCHEMA, "artifact_sha256": self.sha,
                                                    "campaigns": ["/c/0", "/c/1"], "complete": True, "gaps": [gap],
                                                    "advisory": [], "closed": [], "steps": {}})
        report = release._evaluate(self.bundle)
        errors = [e for e in report["errors"] if e.startswith("pass audit:")]
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("pass audit: 01-a#p1: calibration run gpt-6-astra package__x passed o_x"))
        self.assertEqual(report["evidence"]["pass_audit"]["open"], 1)

    def test_qualify_audits_after_the_campaigns_and_before_the_decision(self):
        from pipeline.benchmark.capacity import NativeCapacity
        spec = json.loads((self.bundle / "authoring/spec.json").read_text())
        spec["_producer"] = {"model": "author"}
        atomic_json(self.bundle / "authoring/spec.json", spec)
        ran, order = {}, []

        def native(_bundle, output, *, agent, **kwargs):
            ran[str(output)] = (agent, kwargs.get("model"))
            return {"ok": True}

        def evidence(_bundle, path, role, minimum=1):
            agent, model = ran[str(path)]
            return [], {"model": model, "trials": [{"reward": 1 if agent == "oracle" else 0}]}

        def audited(bundle, sha):
            index = json.loads((Path(bundle) / "verification/native-calibrations.json").read_text())
            order.append(("pass-audit", len(index["campaigns"]), sha == index["artifact_sha256"]))
            return {}

        with ExitStack() as stack:
            for target, value in (
                    ("pipeline.screen.shippable_problems", []), ("pipeline.run.run_bundle", {"verdict": "green"}),
                    ("pipeline.privacy.scan", []), ("pipeline.release._provenance_problems", []),
                    ("pipeline.corpus.problems", []), ("pipeline.assurance.generated_problems", []),
                    ("pipeline.author.controls.problems", []), ("pipeline.benchmark.harbor.doctor", {"ok": True}),
                    ("pipeline.review.review_problems", ["missing review"]), ("pipeline.review.review", {"ok": True})):
                stack.enter_context(mock.patch(target, return_value=value))
            stack.enter_context(mock.patch("pipeline.release._evaluate",
                                           side_effect=lambda b: order.append(("evaluate",)) or {"release_ready": False}))
            stack.enter_context(mock.patch("pipeline.release._pass_audit", side_effect=audited))
            stack.enter_context(mock.patch("pipeline.benchmark.harbor.run_campaign", side_effect=native))
            stack.enter_context(mock.patch("pipeline.evidence.native_evidence", side_effect=evidence))
            stack.enter_context(mock.patch("pipeline.benchmark.harbor.resolve_model", side_effect=lambda m: m))
            stack.enter_context(mock.patch("pipeline.benchmark.capacity.NATIVE_CAPACITY", NativeCapacity(4)))
            stack.enter_context(mock.patch("pipeline.policy.harness_for", side_effect=lambda m: {
                "first-model": "claude-code", "second-model": "codex"}[m]))
            release._qualify(self.bundle, ["first-model", "second-model"], "reviewer", "solver",
                             self.tmp / "native", workers=2)
        self.assertEqual(order, [("pass-audit", 2, True), ("evaluate",)])

    def test_release_repair_collects_the_audit_from_its_receipt_not_from_qualify(self):
        atomic_json(self.bundle / "verification/shipfix-release.json", {"steps": {"qualify": {"ok": True, "result": {
            "errors": ["pass audit: 01-a#p1: calibration run x passed o_x while violating: keep it",
                       "pass audit: unverified, run it again: no docker", "controls: golden scored 0.9"]}}}})
        with mock.patch.object(shipcheck, "coverage", return_value=[]), \
                mock.patch("pipeline.review.review_findings", return_value=[]):
            ids = [b["id"] for b in release_blockers.collect(self.bundle)]
        self.assertEqual(ids, ["QUAL 3"])


if __name__ == "__main__":
    unittest.main()
