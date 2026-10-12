"""The authoring phase catches what the reviewer rejected, without new blockers.

Pilot, 2026-09-28: every first draft cleared the controls and the gates and
failed the independent review for the same shapes. Three things strengthen
the author here and none of them blocks: a shadow-guard runner (no file
budget for the reviewer to call unpublished), over-fit ADVISORIES from
check_draft, and a pre_review tool that runs the real reviewer over the
staged draft before submission.
"""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline.author import agent_designer, checks, validate
from pipeline.author.grader_runtime import grade


def _manifest(repo: Path) -> dict:
    return {p.relative_to(repo).as_posix(): {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size}
            for p in repo.rglob("*") if p.is_file()}


class ShadowGuard(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name) / "repo"
        (self.repo / "app").mkdir(parents=True)
        (self.repo / "app/planner.py").write_text("def plan():\n    return 1\n")
        (self.repo / "app/__init__.py").write_text("")
        self.manifest = _manifest(self.repo)
        self.check = {"runner": {"type": "shadow-guard", "modules": ["planner", "app"]}}

    def test_legitimate_new_files_never_count(self):
        (self.repo / "app/helpers.py").write_text("x = 1\n")
        (self.repo / "NOTES.md").write_text("notes\n")
        (self.repo / "tools").mkdir()
        (self.repo / "tools/convert.py").write_text("y = 2\n")
        ok, why = grade.structural(self.check, self.repo, self.manifest)
        self.assertTrue(ok, why)

    def test_a_planted_top_level_module_named_like_an_import_fails(self):
        (self.repo / "planner.py").write_text("def plan():\n    return 'planted'\n")
        ok, why = grade.structural(self.check, self.repo, self.manifest)
        self.assertFalse(ok)
        self.assertIn("planner.py", why)

    def test_a_planted_package_shadowing_an_import_fails(self):
        (self.repo / "planner").mkdir()
        (self.repo / "planner/__init__.py").write_text("")
        ok, _ = grade.structural(self.check, self.repo, self.manifest)
        self.assertFalse(ok)

    def test_harness_hooks_fail_anywhere(self):
        for name in ("sitecustomize.py", "app/sitecustomize.py", "site.pth", "reward.json"):
            with self.subTest(name=name):
                target = self.repo / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("x\n")
                ok, _ = grade.structural(self.check, self.repo, self.manifest)
                target.unlink()
                self.assertFalse(ok, name)

    def test_an_added_conftest_is_deleted_from_the_run_not_zeroed(self):
        # Arena C-015: an added conftest cannot reach --rootdir /tests/suites,
        # and exclude_added_conftests deletes it from every copy a check runs on.
        for name in ("conftest.py", "app/conftest.py"):
            with self.subTest(name=name):
                target = self.repo / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("x\n")
                ok, why = grade.structural(self.check, self.repo, self.manifest)
                target.unlink()
                self.assertTrue(ok, why)

    def test_a_planted_definition_deeper_in_the_tree_fails(self):
        # tests import `blade_viewer` from the BladeViewer_v2.0 folder; a planted
        # package of that name beside the real module shadows it.
        (self.repo / "BladeViewer_v2.0").mkdir()
        (self.repo / "BladeViewer_v2.0/blade_viewer.py").write_text("real = True\n")
        manifest = _manifest(self.repo)
        check = {"runner": {"type": "shadow-guard", "modules": ["blade_viewer"]}}
        (self.repo / "BladeViewer_v2.0/blade_viewer").mkdir()
        (self.repo / "BladeViewer_v2.0/blade_viewer/__init__.py").write_text("real = False\n")
        ok, why = grade.structural(check, self.repo, manifest)
        self.assertFalse(ok, why)
        # ...while an ordinary helper beside it is fine.
        import shutil
        shutil.rmtree(self.repo / "BladeViewer_v2.0/blade_viewer")
        (self.repo / "BladeViewer_v2.0/geometry_helpers.py").write_text("x = 1\n")
        ok, why = grade.structural(check, self.repo, manifest)
        self.assertTrue(ok, why)

    def test_a_planted_stdlib_package_fails(self):
        for name in ("json/__init__.py", "pytest/__init__.py", "os.py"):
            with self.subTest(name=name):
                target = self.repo / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("hijack = True\n")
                ok, _ = grade.structural(self.check, self.repo, self.manifest)
                target.unlink()
                self.assertFalse(ok, name)

    def test_the_schema_accepts_it_and_the_per_step_rule_is_satisfied(self):
        self.assertEqual(checks.runner_problems({"type": "shadow-guard", "modules": ["planner"]}, [], "g"), [])
        self.assertTrue(checks.runner_problems({"type": "shadow-guard", "modules": "planner"}, [], "g"))
        self.assertIn("shadow-guard", checks.STRUCTURAL)


class OverfitAdvisories(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bundle = Path(temp.name)
        repo = self.bundle / "environment/repo"
        repo.mkdir(parents=True)
        (repo / "ledger.py").write_text("def monthly_totals(rows):\n    return {}\n")
        self.spec = {
            "steps": [{"name": "01-fix", "instruction": "the monthly totals are wrong when a refund lands in a month."}],
            "solution": {"01-fix": {"patch": "--- a/ledger.py\n+++ b/ledger.py\n@@ -1,2 +1,5 @@\n def monthly_totals(rows):\n"
                                             "+    return _fold_refunds(rows)\n+\n+def _fold_refunds(rows):\n+    return {}\n"}},
            "suites": {"test_totals.py": "from ledger import monthly_totals\n\ndef test_refund():\n    assert monthly_totals([]) == {}\n"},
            "checks": [{"id": "g_tree", "kind": "guardrail", "runner": {"type": "tree-guard", "max_new_files": 0},
                        "rule_quote": ["the monthly totals are wrong when a refund lands in a month."]}],
        }

    def test_a_clean_draft_has_only_the_tree_guard_advisory(self):
        out = validate.overfit_advisories(self.spec, self.bundle)
        self.assertEqual(len(out), 1, out)
        self.assertIn("unpublished", out[0])
        self.assertIn("g_tree", out[0])

    def test_a_test_using_a_reference_only_symbol_is_flagged(self):
        self.spec["suites"]["test_totals.py"] += "\nfrom ledger import _fold_refunds\n\ndef test_fold():\n    assert _fold_refunds([]) == {}\n"
        out = validate.overfit_advisories(self.spec, self.bundle)
        hits = [a for a in out if "overfit" in a and "_fold_refunds" in a]
        self.assertEqual(len(hits), 1, out)

    def test_a_symbol_the_instruction_publishes_is_not_flagged(self):
        self.spec["steps"][0]["instruction"] += " call the helper _fold_refunds so i can reuse it."
        self.spec["suites"]["test_totals.py"] += "\nfrom ledger import _fold_refunds\n"
        out = validate.overfit_advisories(self.spec, self.bundle)
        self.assertEqual([a for a in out if "overfit" in a], [])

    def test_a_published_file_rule_silences_the_tree_guard_advisory(self):
        self.spec["checks"][0]["rule_quote"] = ["and dont add new files, keep it in the modules that exist."]
        self.assertEqual([a for a in validate.overfit_advisories(self.spec, self.bundle) if "unpublished" in a], [])

    def test_a_subprocess_stdout_assertion_is_flagged(self):
        self.spec["suites"]["test_cli.py"] = ("import subprocess\n\ndef test_cli():\n    out = subprocess.run(['python', 'main.py'], "
                                              "capture_output=True, text=True)\n    assert 'OK' in out.stdout\n")
        out = validate.overfit_advisories(self.spec, self.bundle)
        self.assertTrue(any("forgeable" in a and "test_cli.py" in a for a in out), out)

    def test_check_draft_carries_advisories_without_blocking(self):
        extraction = SimpleNamespace(session_id="source", seed_index=0, source_root=self.bundle, moments=[])
        handlers = agent_designer._handlers(self.bundle, extraction, {"messages": []}, self.bundle / "draft.json")
        with patch.object(agent_designer.validate_mod, "validate", return_value=[]), \
             patch("pipeline.ledger.faithfulness.citation_problems", return_value=[]):
            verdict = handlers["check_spec"](self.spec)
        self.assertTrue(verdict["clean"], verdict)
        self.assertTrue(any("unpublished" in a for a in verdict["advisories"]), verdict)


class PreReview(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bundle = Path(temp.name)
        (self.bundle / "environment/repo").mkdir(parents=True)
        (self.bundle / "environment/repo/app.py").write_text("print(1)\n")
        extraction = SimpleNamespace(session_id="source", seed_index=0, source_root=self.bundle, moments=[])
        self.log = self.bundle / "authoring/log"
        self.handlers = agent_designer._handlers(self.bundle, extraction, {"messages": []}, self.bundle / "draft.json",
                                                 log=self.log)

    def _fake_review(self, dest, model):
        work = dest / "verification/reviews/x/attempt"
        (work / "deliverables").mkdir(parents=True)
        (work / "deliverables/review.json").write_text(json.dumps({
            "verdict": "revise",
            "checks": [{"id": "o_a", "verdict": "pass", "reason": "fine"},
                       {"id": "o_b", "verdict": "revise", "reason": "the double only answers the reference's flag order"}],
            "ambiguities": [], "substantive_gaps": [{"issue": "step 2 drops step 1 behaviour"}],
            "shortcut_risks": [], "privacy_findings": [],
            "difficulty_assessment": "hard enough", "novelty_assessment": "new"}))
        return {"work_dir": work.relative_to(dest).as_posix(),
                "errors": ["semantic reviewer requested revision", "o_b: check not independently justified"]}

    def test_findings_come_back_and_are_recorded(self):
        with patch.object(agent_designer, "_handlers"):
            pass
        self.handlers["stage_spec"](path=["name"], value="x-task")
        with patch.dict(self.handlers, {}), \
             patch.object(agent_designer, "MODEL", "claude-opus-5"), \
             patch("pipeline.review.review", side_effect=self._fake_review), \
             patch("pipeline.ledger.faithfulness.build", return_value={"schema": "x"}):
            # a draft that fails check_draft is refused before any review is spent
            refused = self.handlers["pre_review"]()
            self.assertIn("error", refused)
            self.assertIn("check_draft", refused["error"])
            with patch.object(agent_designer.validate_mod, "validate", return_value=[]), \
                 patch("pipeline.ledger.faithfulness.citation_problems", return_value=[]), \
                 patch.object(agent_designer.build_mod, "build", lambda spec, dest: (dest / "task.toml").write_text("x")):
                out = self.handlers["pre_review"]()
        self.assertEqual(out["verdict"], "revise", out)
        ids = [f["id"] for f in out["findings"]]
        self.assertEqual(ids, ["o_b", "substantive_gaps/1"])
        self.assertEqual(out["harness_problems"], [])
        from pipeline.config import MAX_PRE_REVIEWS_PER_SESSION
        self.assertEqual(out["remaining"], MAX_PRE_REVIEWS_PER_SESSION - 1)
        record = json.loads((self.log / "pre-review-001.json").read_text())
        self.assertEqual(record["verdict"], "revise")

    def test_the_budget_is_finite_and_never_a_blocker(self):
        from pipeline.config import MAX_PRE_REVIEWS_PER_SESSION
        self.assertGreaterEqual(MAX_PRE_REVIEWS_PER_SESSION, 1)
        self.assertNotIn("pre_review", json.dumps([t for t in agent_designer._tools(0) if t["name"] == "submit_draft"]))
        tool = next(t for t in agent_designer._tools(0) if t["name"] == "pre_review")
        self.assertIn("never requires it", tool["description"])


class ThePromptSaysSo(unittest.TestCase):
    def test_prompt_mentions_the_three_levers(self):
        for phrase in ("pre_review", "shadow-guard guardrail in every step", "ADVISORY check_draft returns"):
            self.assertIn(phrase, agent_designer.SYSTEM, phrase)


if __name__ == "__main__":
    unittest.main()


class ReviewerJsonIsReadUpToOneRepair(unittest.TestCase):
    def test_a_strict_document_is_read_verbatim(self):
        from pipeline.review import load_review_deliverable
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "review.json"
            p.write_text('{"verdict": "pass", "checks": []}')
            doc, repaired = load_review_deliverable(p)
        self.assertEqual(doc["verdict"], "pass")
        self.assertFalse(repaired)

    def test_an_over_escaped_code_quote_is_repaired_and_reported(self):
        from pipeline.review import load_review_deliverable
        raw = '{"checks": [{"id": "o_a", "citations": [{"file": "x.py", "quote": "split(\\\\".\\\\")[-1] == \\\\"core\\\\""}]}]}'
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "review.json"
            p.write_text(raw)
            doc, repaired = load_review_deliverable(p)
        self.assertTrue(repaired)
        self.assertEqual(doc["checks"][0]["citations"][0]["quote"], 'split(".")[-1] == "core"')

    def test_anything_else_is_still_refused(self):
        from pipeline.review import load_review_deliverable
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "review.json"
            p.write_text('{"verdict": "pass", "checks": [}')
            with self.assertRaises(ValueError):
                load_review_deliverable(p)


class TheAnswerReadableInTheFixtureIsCaughtBeforeSubmission(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bundle = Path(temp.name)
        self.repo = self.bundle / "environment/repo"
        self.repo.mkdir(parents=True)
        (self.repo / "core.py").write_text("def total(rows):\n    return sum(rows)\n")
        lines = ["    refunds = [r for r in rows if r.kind == 'refund' and r.month == month]",
                 "    credits = [r for r in rows if r.kind == 'credit' and r.month == month]",
                 "    return sum(r.amount for r in rows) - sum(r.amount for r in refunds)"]
        self.spec = {"solution": {"01-fix": {"patch": "--- a/core.py\n+++ b/core.py\n@@ -1,2 +1,5 @@\n def total(rows):\n"
                                                        + "".join("+" + l + "\n" for l in lines)}}}
        self.lines = lines

    def test_three_reference_lines_in_an_upload_are_a_problem(self):
        (self.repo / "uploads").mkdir()
        (self.repo / "uploads/core_fixed (3).txt").write_text("\n".join(self.lines) + "\n")
        issues = validate._fixture_leak_problems(self.spec, self.bundle)
        self.assertEqual(len(issues), 1, issues)
        self.assertIn("uploads/core_fixed (3).txt", issues[0])

    def test_one_shared_line_is_an_idiom_not_a_leak(self):
        (self.repo / "notes.txt").write_text(self.lines[0] + "\n")
        self.assertEqual(validate._fixture_leak_problems(self.spec, self.bundle), [])


class TheSessionCapFitsTheStrengthenedWorkflow(unittest.TestCase):
    def test_cap(self):
        self.assertGreaterEqual(agent_designer.MAX_STEPS, 200)


class TheReviewerKnowsWhatAShadowGuardIs(unittest.TestCase):
    def test_prompt_clause(self):
        from pipeline.review import PROMPT
        self.assertIn("HARNESS GUARDS ARE NOT REQUIREMENTS", PROMPT)
        self.assertIn("shadow-guard", PROMPT)


class OrphanedSandboxesAreReaped(unittest.TestCase):
    def test_only_old_containers_are_selected(self):
        from pipeline.author import sandbox
        import calendar
        from datetime import datetime
        now = calendar.timegm(datetime(2026, 9, 29, 0, 45, 0).timetuple())
        out = ("ae991e23bd52 2026-09-27 11:57:04 +0000 UTC\n"
               "39e15ce8bea8 2026-09-28 12:22:14 +0000 UTC\n"
               "1864e78a056f 2026-09-29 00:44:50 +0000 UTC\n"
               "garbage line\n")
        self.assertEqual(sandbox._stale_container_ids(out, now), ["ae991e23bd52", "39e15ce8bea8"])
        self.assertEqual(sandbox._stale_container_ids(out, now, max_age_s=10 * 24 * 3600), [])


class _FakeContainer:
    """A container whose shell answers from a script; records what was asked."""
    def __init__(self, files: dict[str, str]):
        self.files = dict(files)
        self.commands: list[str] = []
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def exec(self, cmd, *, user=None, env=None, timeout=None, workdir=None):
        self.commands.append(cmd)
        if cmd.startswith("cat "):
            rel = cmd[4:].replace("/app/repo/", "")
            return {"exit_code": 0 if rel in self.files else 1, "stdout": self.files.get(rel, ""), "stderr": "" if rel in self.files else "no such file", "timed_out": False}
        if "diff -ruN" in cmd:
            body = self.files.get("core.py", "")
            # The enclosing shell treats diff's status 1 as a successful capture.
            return {"exit_code": 0, "stdout": "--- a/core.py\n+++ b/core.py\n@@ -1 +1 @@\n-old\n+" + body.strip() + "\n", "stderr": "", "timed_out": False}
        if "find ." in cmd:
            return {"exit_code": 0, "stdout": "\n".join("./" + k for k in self.files), "stderr": "", "timed_out": False}
        return {"exit_code": 0, "stdout": "ok", "stderr": "", "timed_out": False}
    def put_bytes(self, data, guest, mode=0o644):
        self.files[guest.replace("/app/repo/", "")] = data.decode()
    def capture(self, guest_root, members, excludes, destination):
        # the diff the fake container "wrote" to /tmp/d/blind.patch
        body = self.files.get("core.py", "")
        Path(destination).mkdir(parents=True, exist_ok=True)
        big = "--- a/core.py\n+++ b/core.py\n@@ -1 +1 @@\n-old\n+" + body.strip() + "\n" + ("+# padding\n" * 3000)
        (Path(destination) / "blind.patch").write_text(big)
        return {"files": 1}


class TheIndependentSolverControl(unittest.TestCase):
    def _package(self, root: Path) -> Path:
        pkg = root / "pkg"
        (pkg / "steps/01-fix").mkdir(parents=True)
        (pkg / "steps/01-fix/instruction.md").write_text("the totals are wrong, fix core.py so refunds count.\n")
        (pkg / "environment/repo").mkdir(parents=True)
        (pkg / "environment/repo/core.py").write_text("old\n")
        (pkg / "tests").mkdir()
        (pkg / "tests/checks.json").write_text("{}")
        (pkg / "task.toml").write_text("[task]\nname='x'\n[[steps]]\nname='01-fix'\n")
        return pkg

    def test_openai_loop_runs_tools_until_the_model_stops(self):
        from pipeline.author import blind_solve as bs
        container = _FakeContainer({"core.py": "old\n"})
        tools = bs.SandboxTools(container)
        responses = [
            {"status": "completed", "usage": {"input_tokens": 10}, "output": [
                {"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": json.dumps({"path": "core.py"})}]},
            {"status": "completed", "usage": {"input_tokens": 12}, "output": [
                {"type": "function_call", "call_id": "c2", "name": "write_file", "arguments": json.dumps({"path": "core.py", "content": "new\n"})},
                {"type": "function_call", "call_id": "c3", "name": "run", "arguments": json.dumps({"command": "python -c 1"})}]},
            {"status": "completed", "usage": {"input_tokens": 3}, "output": [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}]},
        ]
        it = iter(responses)
        session = bs._openai_loop("fix it", "gpt-6-astra", tools, 10, "high", lambda body: next(it))
        self.assertEqual(session["steps"], 3)
        self.assertEqual(session["usage"], {"input_tokens": 25})
        self.assertEqual(tools.calls, 3)
        self.assertEqual(container.files["core.py"], "new\n")
        outputs = [i for i in session["conversation"] if i.get("type") == "function_call_output"]
        self.assertEqual(len(outputs), 3)
        self.assertIn('"exit_code": 0', outputs[2]["output"])

    def test_paths_cannot_escape_the_repository(self):
        from pipeline.author import blind_solve as bs
        tools = bs.SandboxTools(_FakeContainer({}))
        with self.assertRaises(ValueError):
            tools.call("read_file", {"path": "../etc/passwd"})
        with self.assertRaises(ValueError):
            tools._guest("/etc/passwd")
        with self.assertRaises(ValueError):
            tools._guest("a/../../b")

    def test_solve_grades_the_diff_and_records_the_shape(self):
        from pipeline.author import blind_solve as bs
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = self._package(root)
            container = _FakeContainer({"core.py": "old\n"})
            responses = iter([
                {"status": "completed", "output": [{"type": "function_call", "call_id": "c1", "name": "write_file",
                                                     "arguments": json.dumps({"path": "core.py", "content": "new\n"})}]},
                {"status": "completed", "output": []},
            ])
            graded = {"reward": 0.5, "valid": True, "steps": {"01-fix": {"reward": 0.5, "grade": {"checks": {
                "o_a": {"passed": True}, "o_b": {"passed": False, "reason": "assert 1 == 2", "stderr": "boom"}}}}}}
            with patch("pipeline.author.sandbox.verify", return_value=graded) as verify, \
                 patch("pipeline.author.sandbox.build_image", return_value="tag"):
                record = bs.solve(pkg, "gpt-6-astra", root / "work", max_steps=5,
                                  call=lambda body: next(responses), container_factory=lambda: container)
            self.assertEqual(record["model"], "gpt-6-astra")
            self.assertEqual(record["reward"], 0.5)
            self.assertEqual(record["checks"], {"o_a": True, "o_b": False})
            self.assertEqual(list(record["failing"]), ["o_b"])
            text = (root / "work/blind.patch").read_text()
            self.assertTrue(text.startswith("--- a/core.py"))
            self.assertGreater(len(text), 20000, "the whole diff is kept, not the exec tail")
            variants = verify.call_args.args[1]
            self.assertEqual(list(variants), ["01-fix"])
            self.assertEqual(variants["01-fix"].kind, "patch")

    def test_the_solver_is_a_different_vendor(self):
        from pipeline.author import blind_solve as bs
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("ARENA_BLIND_SOLVER_MODEL", None)
            self.assertTrue(bs.solver_model_for("claude-opus-5").startswith("gpt-"))
            self.assertTrue(bs.solver_model_for("gpt-6-astra").startswith("claude-"))

    def test_the_designer_tool_is_advisory_and_records_the_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            (bundle / "environment/repo").mkdir(parents=True)
            (bundle / "environment/repo/app.py").write_text("print(1)\n")
            extraction = SimpleNamespace(session_id="source", seed_index=0, source_root=bundle, moments=[])
            log = bundle / "authoring/log"
            handlers = agent_designer._handlers(bundle, extraction, {"messages": []}, bundle / "draft.json", log=log)
            handlers["stage_spec"](path=["name"], value="x-task")
            record = {"schema": "arena-coding/blind-solve/1", "model": "gpt-6-astra", "step": "01-fix", "steps": 4,
                      "tool_calls": 3, "usage": {}, "patch_bytes": 40, "seconds": 1.0, "reward": 0.4,
                      "checks": {"o_a": True, "o_b": False}, "failing": {"o_b": {"reason": "x", "stderr": ""}}}
            with patch.object(agent_designer.sandbox, "available", return_value=(True, "ok")), \
                 patch.object(agent_designer.validate_mod, "validate", return_value=[]), \
                 patch("pipeline.ledger.faithfulness.citation_problems", return_value=[]), \
                 patch.object(agent_designer.build_mod, "build", lambda spec, dest: (dest / "task.toml").write_text("x")), \
                 patch("pipeline.author.blind_solve.solve", return_value=dict(record)):
                out = handlers["blind_solve"]()
            self.assertEqual(out["checks"], {"o_a": True, "o_b": False})
            self.assertIn("TRAP", out["how_to_read"])
            # The budget is the baseline difficulty loop's (policy.BASELINE_BLIND_SOLVES, 2026-10-04).
            self.assertEqual(out["remaining"], agent_designer._blind_budget() - 1)
            self.assertTrue((bundle / "verification/blind-solve.json").is_file())
            self.assertTrue((bundle / "verification/blind-solve-astra.json").is_file())
            self.assertTrue((log / "blind-solve-001.json").is_file())
            tool = next(t for t in agent_designer._tools(0) if t["name"] == "blind_solve")
            self.assertIn("never required", tool["description"])
            self.assertIn("blind_solve", agent_designer.SYSTEM)
            from pipeline.review import PROMPT
            self.assertIn("blind-solve.json", PROMPT)


class TheVerifierIsolatesHiddenTests(unittest.TestCase):
    def test_the_image_makes_tests_read_only_and_creates_the_grader_user(self):
        from pipeline.author import toolchain
        text = toolchain.verifier_dockerfile({"name": "python", "apt": [], "env": {}})
        self.assertIn("chmod -R a-w /tests", text)
        self.assertIn("grader:x:2001:2001", text)
        self.assertIn("COPY . /tests/", text)

    def test_privileges_are_dropped_only_when_root_and_the_user_exists(self):
        from unittest.mock import patch
        import pwd
        entry = pwd.struct_passwd(("grader", "x", 2001, 2001, "", "/", "/usr/sbin/nologin"))
        with patch("os.geteuid", return_value=1000):
            self.assertIsNone(grade._grader_ids())
        with patch("os.geteuid", return_value=0), patch("pwd.getpwnam", return_value=entry):
            self.assertEqual(grade._grader_ids(), (2001, 2001))
        with patch("os.geteuid", return_value=0), patch("pwd.getpwnam", side_effect=KeyError):
            self.assertIsNone(grade._grader_ids())
        self.assertIsNone(grade._drop_privileges(None))
        self.assertTrue(callable(grade._drop_privileges((2001, 2001))))

    def test_chown_covers_files_dirs_and_symlinks_without_following(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "a/f.py").write_text("x")
            (root / "link").symlink_to("a/f.py")
            seen = []
            with patch("os.lchown", side_effect=lambda p, u, g: seen.append(os.path.relpath(p, root))):
                grade._chown_tree(root, (2001, 2001))
            self.assertEqual(sorted(seen), [".", "a", "a/f.py", "link"])
