"""The unattended loop ships Arena v0.1 (2026-10-30 audit: the driver released without --arena-v01 and never ran
Arena's checker). Language detection, domain slugs, the driver's release arguments and the held state."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import arena_v01_release as v01


def bundle_with(text: str) -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "steps/01-task").mkdir(parents=True)
    (root / "steps/01-task/instruction.md").write_text(text, encoding="utf-8")
    return root


class Language(unittest.TestCase):
    def test_a_model_answer_is_checked_against_arenas_pattern(self):
        b = bundle_with("Сделай страницу с колесом фортуны.")
        self.assertEqual(v01.detect_prompt_language(b, ask=lambda text: "ru\n"), "ru")
        self.assertEqual(v01.detect_prompt_language(b, ask=lambda text: "`pt-BR`"), "pt-BR")

    def test_without_an_answer_only_an_unambiguous_script_decides(self):
        self.assertEqual(v01.detect_prompt_language(bundle_with("ゲームのデータを整理してください。"),
                                                    ask=lambda t: (_ for _ in ()).throw(RuntimeError())), "ja")
        with self.assertRaises(ValueError):
            v01.detect_prompt_language(bundle_with("Build the report."), ask=lambda t: "English please")

    def test_domains_become_snake_case(self):
        self.assertEqual(v01.domain_slug("html/js single-page app"), "html_js_single_page_app")
        self.assertEqual(v01.domain_slug("sports_analytics"), "sports_analytics")
        with self.assertRaises(ValueError):
            v01.domain_slug("—")


class Driver(unittest.TestCase):
    def test_releases_carry_the_v01_target_and_unconfigured_batches_are_held(self):
        from orchestration import driver
        row = {"lineage": "/x/envs-t1/target-F2-scenario/c-001-wheel",
               "versions": [{"path": "/x/v1", "session_id": "s1", "seed_index": 0}]}
        cfg = {"batches": {"envs-t1": {"wave": "w.json"}}}
        with mock.patch.object(driver, "CFG", cfg), \
                mock.patch.object(driver, "wave_row", return_value={"classification": {"domain": "spin wheel app"}}):
            args = driver.v01_args(row, "/x/v1")
        self.assertEqual(args[0], "--arena-v01")
        self.assertEqual(json.loads(args[1]), {"domain": "spin wheel app", "prompt_language": "auto"})
        with mock.patch.object(driver, "CFG", {"batches": {"envs-t1": {"arena_v01": False}}}):
            self.assertEqual(driver.v01_args(row, "/x/v1"), [])
        state = {"lineages": {"k": {}}}
        with mock.patch.object(driver, "CFG", cfg), mock.patch.object(driver, "log"), \
                mock.patch("subprocess.run") as run:
            driver.release_finish([{"name": "c-001-wheel", "bundle": "/x/v1", "ws": "ws", "key": "k",
                                    "tree": "envs-t1", "arena_v01": True}], state)
        run.assert_not_called()
        self.assertIn("archive", state["lineages"]["k"]["package_held"])


if __name__ == "__main__":
    unittest.main()


class TopUps(unittest.TestCase):
    def test_trials_and_measurements_concatenate_a_campaigns_parts(self):
        from pipeline import arena_v01_package as ap
        root = Path(tempfile.mkdtemp())
        for part, ids in (("primary", ("t1", "t2")), ("topup", ("t3",))):
            for i in ids:
                (root / part / "jobs/run" / i).mkdir(parents=True)
                (root / part / "jobs/run" / i / "result.json").write_text("{}")
            (root / part / "report.json").write_text(json.dumps({"trials": [{"id": i, "valid": True, "steps": []}
                                                                             for i in ids]}))
        self.assertEqual([t.name for t in ap._trials([root / "primary", root / "topup"])], ["t1", "t2", "t3"])
        self.assertEqual([t.name for t in ap._trials(root / "primary")], ["t1", "t2"])
        from orchestration import arena_v01_ship as ship
        self.assertEqual([t["id"] for t in ship._measured([root / "primary", root / "topup"])["trials"]],
                         ["t1", "t2", "t3"])


class DesignRerun(unittest.TestCase):
    """2026-10-30: c-001-html-js died at design on five `_StreamInterrupted` attempts and was listed as building."""

    def row(self, error, status="error", alive=False):
        return {"lineage": "/x/envs-t1/target-F3-replay/c-001-wheel", "ws": "ws", "busy": [], "tree_build_alive": alive,
                "versions": [{"task": False, "status": status, "stage": "design", "error": error,
                              "session_id": "s1", "seed_index": 0}]}

    def test_an_infrastructure_error_is_resumed_twice_then_blocked(self):
        from orchestration import driver
        entry = {"actions": []}
        error = "RuntimeError: design failed 1 times. attempt 1: designer call failed after 5 attempts: x"
        with mock.patch.object(driver, "wave_rank", return_value=3), mock.patch.object(driver, "WAVES", {"envs-t1": "w.json"}), \
                mock.patch.object(driver, "launch", return_value="run-1") as launch:
            self.assertEqual(driver.design_rerun(self.row(error), entry, False), "rerun")
            self.assertEqual(driver.design_rerun(self.row(error), entry, False), "rerun")
            self.assertEqual(driver.design_rerun(self.row(error), entry, False), "blocked")
        argv = launch.call_args.args[1]
        self.assertEqual(argv[:3], ["build", "-o", "/x/envs-t1"])
        self.assertEqual(argv[argv.index("--ranks") + 1], "3")

    def test_alive_builds_account_loss_and_design_defects_are_not_rerun(self):
        from orchestration import driver
        with mock.patch.object(driver, "launch") as launch:
            self.assertEqual(driver.design_rerun(self.row("designer call failed after 5", alive=True),
                                                 {"actions": []}, False), "building")
            self.assertEqual(driver.design_rerun(self.row("", status="running", alive=True), {"actions": []}, False),
                             "building")
            self.assertEqual(driver.design_rerun(self.row("designer call failed after 5 attempts: 401 Unauthorized"),
                                                 {"actions": []}, False), "blocked")
            self.assertEqual(driver.design_rerun(self.row("ValueError: fixture placeholder"), {"actions": []}, False),
                             "blocked")
        launch.assert_not_called()

    def test_a_build_that_died_running_is_resumed(self):
        from orchestration import driver
        with mock.patch.object(driver, "wave_rank", return_value=1), mock.patch.object(driver, "WAVES", {"envs-t1": "w"}), \
                mock.patch.object(driver, "launch", return_value="run-2"):
            self.assertEqual(driver.design_rerun(self.row("", status="running"), {"actions": []}, False), "rerun")

    def test_the_probe_reports_a_live_build_of_the_tree(self):
        from orchestration import driver
        self.assertIn('"tree_build_alive"', driver.PROBE)


class LanguageCallBudget(unittest.TestCase):
    def test_the_language_call_leaves_room_for_thinking(self):
        """2026-10-30: at max_tokens 32 Opus 5.5 returned only a thinking block and every Latin-script task was refused."""
        from unittest import mock
        seen = {}

        def fake(body):
            seen.update(body)
            return {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "pt-BR"}]}
        with mock.patch("pipeline.select.classify._call", fake):
            self.assertEqual(v01._ask_language("Preciso que você revise o deck."), "pt-BR")
        self.assertGreaterEqual(seen["max_tokens"], 512)


class LanguageFailureNamed(unittest.TestCase):
    def test_a_failed_language_call_is_named_in_the_refusal(self):
        with self.assertRaisesRegex(ValueError, "the language call failed: RuntimeError: no Anthropic credential"):
            v01.detect_prompt_language(bundle_with("Preciso que você revise o deck."),
                                       ask=lambda t: (_ for _ in ()).throw(RuntimeError("no Anthropic credential")))
