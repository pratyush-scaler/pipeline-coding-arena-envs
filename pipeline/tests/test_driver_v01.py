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
