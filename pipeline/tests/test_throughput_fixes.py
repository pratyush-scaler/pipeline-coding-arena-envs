"""Losses and waits the 2026-10-03 Coder waves exposed, each pinned.

Census at 2026-10-04 00:00: 115 seeds, 24 validated, 26 lost -- of which 15 to
pipeline defects rather than task defects -- and every design about 6.5 hours,
three quarters of it waiting on pre-reviews, serial controls and sandbox runs.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline import harden, orchestrate, policy, review
from pipeline.author import agent_designer, controls, sandbox


class ReviewDeliverableEscapes(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write(text)
        return review.load_review_deliverable(Path(handle.name))

    def test_a_raw_regex_escape_is_read_as_the_reviewer_wrote_it(self):
        doc, repaired = self.load(r'{"reason": "matches \d+ then \s"}')
        self.assertEqual(doc["reason"], r"matches \d+ then \s")
        self.assertTrue(repaired)

    def test_valid_escapes_are_untouched_and_not_reported(self):
        doc, repaired = self.load(r'{"reason": "tab\t quote\" slash\\d é"}')
        self.assertEqual(doc["reason"], 'tab\t quote" slash\\d é')
        self.assertFalse(repaired)

    def test_both_repairs_together(self):
        doc, _ = self.load(r'{"reason": "split(\\".\\") and \d"}')
        self.assertEqual(doc["reason"], r'split(".") and \d')

    def test_anything_else_is_still_refused(self):
        with self.assertRaises(ValueError):
            self.load('{"reason": "unterminated}')


class PreflightFeedback(unittest.TestCase):
    def test_contract_errors_and_failing_checks_reach_the_designer(self):
        records = {"golden-1": {"steps": {"01-s": {"reward": 0.0, "grade": {
            "contract_errors": ["declared deliverable app is missing, nonregular, linked, or invalid: "
                                "declared artifact must be a regular file"] * 2,
            "checks": {"o_a": {"passed": False, "reason": "contract error"},
                       "o_b": {"passed": True}}}}}},
            "nop": {"steps": {"01-s": {"reward": 0.0, "grade": {"contract_errors": [], "checks": {}}}}}}
        lines = orchestrate.preflight_feedback(SimpleNamespace(records=records, errors=["golden-1: x"]))
        self.assertEqual(len(lines), 1)                    # a clean nop is not a finding
        self.assertIn("golden-1/01-s", lines[0])
        self.assertIn("must be a regular file", lines[0])
        self.assertEqual(lines[0].count("must be a regular file"), 1)
        self.assertIn("o_a (contract error)", lines[0])
        self.assertNotIn("o_b", lines[0])

    def test_falls_back_to_the_raw_errors(self):
        lines = orchestrate.preflight_feedback(SimpleNamespace(records={}, errors=["golden-1: incomplete"]))
        self.assertEqual(lines, ["control preflight: golden-1: incomplete"])


class RevisionMayReaimObjectives(unittest.TestCase):
    before = {"checks": [{"id": "o_floor", "kind": "objective", "weight": 5},
                         {"id": "o_band", "kind": "objective", "weight": 5},
                         {"id": "g_guard", "kind": "guardrail", "weight": 0}],
              "steps": [{"name": "01"}]}

    def revised(self, **changes):
        checks = [dict(c, **changes.get(c["id"], {})) for c in self.before["checks"]]
        return {"checks": checks, "steps": [{"name": "01"}]}

    def test_an_in_band_objective_may_change(self):
        targets = [{"id": "o_floor"}]
        self.assertEqual(harden.revision_problems(self.before, self.revised(o_band={"weight": 9}), targets), [])

    def test_a_guardrail_still_may_not(self):
        problems = harden.revision_problems(self.before, self.revised(g_guard={"weight": 1}), [{"id": "o_floor"}])
        self.assertTrue(any(p.startswith("g_guard: revision changed an untargeted check") for p in problems))

    def test_dependency_chains_and_removals_are_still_refused(self):
        problems = harden.revision_problems(self.before, self.revised(o_band={"requires": ["o_floor"]}), [])
        self.assertTrue(any("dependency chains" in p for p in problems))
        removed = {"checks": self.before["checks"][:1], "steps": [{"name": "01"}]}
        self.assertTrue(any("removes measured checks" in p for p in harden.revision_problems(self.before, removed, [])))

    def test_a_new_lineage_still_keeps_every_prior_check(self):
        after = self.revised(o_band={"weight": 9})
        after["steps"] = [{"name": "01"}, {"name": "02"}]
        self.assertTrue(any(p.startswith("o_band: revision changed an untargeted check")
                            for p in harden.revision_problems(self.before, after, [], new_lineage=True)))


class DesignerCallRetries(unittest.TestCase):
    def test_a_stream_interruption_streak_is_retried_after_a_pause(self):
        calls = []

        def flaky(body):
            calls.append(body)
            if len(calls) < 3:
                raise RuntimeError("designer call failed after 5 attempts: Anthropic transport interrupted (_StreamInterrupted)")
            return {"content": []}
        with patch.object(agent_designer, "_call", flaky), patch.object(agent_designer.time, "sleep") as sleep:
            self.assertEqual(agent_designer._designer_call({"x": 1}), {"content": []})
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_an_unlisted_mid_stream_error_is_retried(self):
        outcomes = [RuntimeError("Anthropic stream reported a provider error"), {"ok": True}]

        def call(body):
            item = outcomes.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        with patch.object(agent_designer, "_call", call), patch.object(agent_designer.time, "sleep"):
            self.assertEqual(agent_designer._designer_call({}), {"ok": True})

    def test_a_request_that_can_never_pass_is_not(self):
        from pipeline.provider import QUOTA_MARKER
        for text in ("HTTP 400: provider request failed", f"{QUOTA_MARKER}: spent"):
            calls = []

            def fatal(body, text=text):
                calls.append(1)
                raise RuntimeError(text)
            with self.subTest(text=text), patch.object(agent_designer, "_call", fatal), \
                    patch.object(agent_designer.time, "sleep"):
                with self.assertRaises(RuntimeError):
                    agent_designer._designer_call({})
            self.assertEqual(len(calls), 1)

    def test_the_streak_is_bounded(self):
        calls = []

        def always(body):
            calls.append(1)
            raise RuntimeError("Anthropic transport interrupted")
        with patch.object(agent_designer, "_call", always), patch.object(agent_designer.time, "sleep"):
            with self.assertRaises(RuntimeError):
                agent_designer._designer_call({})
        self.assertEqual(len(calls), agent_designer.DESIGNER_CALL_ROUNDS)

    def test_the_transport_file_is_not_where_this_lives(self):
        # transport.py is hashed into artifacts.harness_identity; editing it
        # stales every recorded semantic review.
        source = (Path(agent_designer.__file__).parent / "transport.py").read_text()
        self.assertNotIn("_FINAL_STREAM_ERRORS", source)


class SandboxImagePresence(unittest.TestCase):
    def test_a_pruned_image_is_rebuilt_before_use(self):
        with patch.object(sandbox, "_image_exists", return_value=False), \
                patch.object(sandbox, "build_image", return_value="arena-coding-sandbox-agent:new") as build:
            self.assertEqual(sandbox.present_image("arena-coding-sandbox-agent:old", Path("/b"), "agent"),
                             "arena-coding-sandbox-agent:new")
        build.assert_called_once_with(Path("/b"), "agent")

    def test_a_present_image_is_used_as_is(self):
        with patch.object(sandbox, "_image_exists", return_value=True), \
                patch.object(sandbox, "build_image") as build:
            self.assertEqual(sandbox.present_image("t:1", Path("/b"), "verifier"), "t:1")
        build.assert_not_called()


class ControlWorkers(unittest.TestCase):
    """2026-10-26: the count is derived from the machine unless the environment sets it."""

    def machine(self, cores, mem_gb, load):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.dict("os.environ", {"ARENA_CONTROL_WORKERS": ""}))
        stack.enter_context(patch.object(controls.os, "cpu_count", return_value=cores))
        stack.enter_context(patch.object(controls, "_memory_gb", return_value=mem_gb))
        stack.enter_context(patch.object(controls.os, "getloadavg", return_value=(load, load, load)))
        return stack

    def test_the_environment_sets_the_default_within_the_cap(self):
        with patch.dict("os.environ", {"ARENA_CONTROL_WORKERS": "3"}):
            self.assertEqual(controls.default_workers(), 3)
        with patch.dict("os.environ", {"ARENA_CONTROL_WORKERS": "40"}):
            self.assertEqual(controls.default_workers(), policy.MAX_CONTROL_WORKERS)
        with patch.dict("os.environ", {"ARENA_CONTROL_WORKERS": "lots"}), self.machine(4, 8, 0.0):
            self.assertEqual(controls.default_workers(), 1)

    def test_the_machine_derives_the_default(self):
        with self.machine(32, 128, 2.0):
            self.assertEqual(controls.default_workers(), 8)       # cores / 4, under the cap
        with self.machine(32, 40, 2.0):
            self.assertEqual(controls.default_workers(), 4)       # RAM / 9 GB binds
        with self.machine(32, 128, 30.0):
            self.assertEqual(controls.default_workers(), 4)       # halved above 85% load
        with self.machine(4, 8, 0.0):
            self.assertEqual(controls.default_workers(), 1)       # a laptop
        with self.machine(64, 256, 0.0):
            self.assertEqual(controls.default_workers(), policy.MAX_CONTROL_WORKERS)

    def test_coder_runs_leave_the_count_to_the_machine(self):
        from pipeline.benchmark import coder_run
        self.assertNotIn("ARENA_CONTROL_WORKERS", coder_run.DEFAULT_REMOTE_ENV)
        self.assertLessEqual(3, policy.MAX_CONTROL_WORKERS)


class OracleEvidenceIsNotDrift(unittest.TestCase):
    def test_generated_problems_carries_oracle_evidence_into_the_rebuild(self):
        from pipeline import assurance
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "c-001-x"
            (bundle / "environment/repo").mkdir(parents=True)
            (bundle / "authoring").mkdir()
            (bundle / "authoring/spec.json").write_text("{}")
            oracle = bundle / "authoring/oracles/abc"
            oracle.mkdir(parents=True)
            (oracle / "receipt.json").write_text("{}")
            seen = {}

            def build(spec, scratch):
                seen["oracle"] = (Path(scratch) / "authoring/oracles/abc/receipt.json").is_file()
            with patch.object(assurance.build_mod, "build", build):
                problems = assurance.generated_problems(bundle)
            self.assertTrue(seen["oracle"])
            self.assertFalse([p for p in problems if "authoring/oracles" in p])


class ScreenAsBuilt(unittest.TestCase):
    def test_a_validated_task_is_screened_before_the_batch_ends(self):
        order = []
        rows = [{"session_id": "s", "seed_index": 0}, {"session_id": "t", "seed_index": 0}]

        def build_one(row, i, out_root, designer, budget):
            order.append(f"built {i}")
            status = "validated" if i == 1 else "blocked"
            return orchestrate.TaskResult(slug=f"c-00{i}-x", session_id=row["session_id"], seed_index=0,
                                          status=status, error="", creation_status="complete", design_attempts=1)

        def screen(bundle):
            order.append(f"screened {bundle.name}")
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(orchestrate, "build_one", build_one), \
                patch.object(orchestrate, "_bundle_for", lambda out, row, slug: Path(out) / slug), \
                patch.object(orchestrate, "_screen_as_built", screen):
            report = orchestrate.batch(rows, Path(tmp), designer=None, task_workers=1, screen_workers=2)
        self.assertIn("screened c-001-x", order)
        self.assertNotIn("screened c-002-x", order)
        self.assertEqual(report["validated"], 1)

    def test_without_screen_workers_nothing_is_screened(self):
        def build_one(row, i, out_root, designer, budget):
            return orchestrate.TaskResult(slug="c-001-x", session_id="s", seed_index=0, status="validated",
                                          error="", creation_status="complete", design_attempts=1)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(orchestrate, "build_one", build_one), \
                patch.object(orchestrate, "_screen_as_built") as screen:
            orchestrate.batch([{"session_id": "s", "seed_index": 0}], Path(tmp), designer=None, task_workers=1)
        screen.assert_not_called()

    def test_the_policy_reads_it(self):
        self.assertIs(policy.SCREEN_AS_BUILT, True)


class DirectoryDeliverables(unittest.TestCase):
    def test_validation_names_a_declared_directory(self):
        import hashlib
        from pipeline.artifacts import atomic_json
        from pipeline.author import validate
        from pipeline.tests import synthetic_code
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provenance = {}
            for rel, text in {**synthetic_code.repo_files(), "public/index.html": "<p>x</p>\n"}.items():
                path = root / "environment/repo" / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                provenance[rel] = {"reason": "Synthetic source for testing",
                                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            atomic_json(root / "authoring/extraction/fixture-authored.json", {"files": provenance})
            spec = synthetic_code.spec()
            step = spec["steps"][0]["name"]
            spec["deliverables"] = [{"path": "public", "kind": "file", "step": step}]
            self.assertTrue(any("is a directory in the fixture" in i for i in validate.validate(spec, root)))
            spec["deliverables"] = [{"path": "public/index.html", "kind": "file", "step": step}]
            self.assertFalse(any("is a directory in the fixture" in i for i in validate.validate(spec, root)))


if __name__ == "__main__":
    unittest.main()


class UntouchedModelBrief(unittest.TestCase):
    def test_a_model_that_failed_nothing_gets_its_own_levers(self):
        record = {"outlook": {"signals": {"live_edges": {"anthropic/claude-fable-5-1": ["01/o_x"],
                                                         "openai/gpt-6-astra": []}}}}
        text = "\n".join(harden._untouched_model_text(record))
        self.assertIn("openai/gpt-6-astra FAILED NO CHECK", text)
        self.assertIn("case_family_from_reproducer", text)
        self.assertNotIn("claude-fable-5-1 FAILED", text)
        self.assertNotIn("rule_scope_overreach", text)       # a Fable lever

    def test_nothing_when_both_models_have_live_edges(self):
        record = {"outlook": {"signals": {"live_edges": {"a/x": ["c"], "openai/y": ["d"]}}}}
        self.assertEqual(harden._untouched_model_text(record), [])


class UntouchedModelFromCounts(unittest.TestCase):
    def test_an_always_failed_check_is_not_untouched(self):
        record = {"per_model": {"openai/gpt-6-astra": {"checks": {"01/o_a": {"passed": 0, "failed": 5}}},
                                "anthropic/claude-fable-5-1": {"checks": {"01/o_a": {"passed": 5, "failed": 0}}}},
                  "outlook": {"signals": {"live_edges": {"openai/gpt-6-astra": []}}}}
        text = "\n".join(harden._untouched_model_text(record))
        self.assertNotIn("gpt-6-astra FAILED NO CHECK", text)
        self.assertIn("claude-fable-5-1 FAILED NO CHECK", text)
