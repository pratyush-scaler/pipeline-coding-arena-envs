"""The ship-time sweep finds each class Arena's review found (2026-10-05), and only those.

Every positive case is the shape of a real finding: C-002/C-032 (login shell
loses the venv), C-015 (pytest declared, not installed), C-020 (README says Go
1.26+ on a 1.23 image), C-036 (an advertised script reads /home/user/...),
C-030 (a PHP include three levels above the repository), C-028 (excluded demo
media named by library.json), the verification-library guardrail (10/10
episodes failed), and C-006/C-013 (a counter-implementation scoring 1.0).
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pipeline import coverage_critic, policy, shipcheck
from pipeline.author import toolchain


class Bundle(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.b = self.root / "F2-scenario" / "a_f2_x"
        (self.b / "environment/repo").mkdir(parents=True)
        (self.b / "environment/deps").mkdir(parents=True)
        self.chain("python")
        self.ledger([])

    def chain(self, name, **kw):
        doc = toolchain.record(name, language=name, **kw)
        (self.b / "environment/toolchain.json").write_text(json.dumps(doc))

    def ledger(self, omissions, level="F2"):
        (self.b / "provenance").mkdir(exist_ok=True)
        (self.b / "provenance/ledger.json").write_text(json.dumps(
            {"recovery_omissions": omissions, "faithfulness_level": level}))

    def repo(self, rel, text):
        path = self.b / "environment/repo" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


class PackageDetectors(Bundle):
    def test_login_shell_flags_toolchains_whose_binaries_leave_the_login_path(self):
        self.assertEqual([f["check"] for f in shipcheck.login_shell(self.b)], ["image.login_shell"])
        self.chain("go")
        self.assertIn("/usr/local/go/bin", shipcheck.login_shell(self.b)[0]["detail"])
        self.chain("node")
        self.assertEqual(shipcheck.login_shell(self.b), [])
        self.chain("python", login_shell_path=True)
        self.assertEqual(shipcheck.login_shell(self.b), [])

    def test_declared_python_packages_must_be_installed(self):
        self.repo("legal_os/backend/requirements.txt", "fastapi==0.110\npytest==7.4.2\nalembic>=1\n# c\n")
        (self.b / "environment/deps/requirements.txt").write_text("fastapi==0.110.0\n")
        found = shipcheck.deps_declared(self.b)
        self.assertEqual(len(found), 1)
        self.assertIn("alembic, pytest", found[0]["detail"])
        self.assertEqual((found[0]["route"], found[0]["measure"]), ("fixture", "screen"))
        (self.b / "environment/deps/requirements.txt").write_text("fastapi==0.110.0\npytest==7.4.2\nalembic==1.13\n")
        self.assertEqual(shipcheck.deps_declared(self.b), [])

    def test_packages_a_post_install_step_pins_count_as_installed(self):
        self.repo("package.json", json.dumps({"dependencies": {"next": "^15.1.3", "@types/node": "^22"},
                                              "devDependencies": {"eslint": "^9"}}))
        self.repo("api/requirements.txt", "fastapi>=0.110\nuvicorn\n")
        (self.b / "environment/deps/package.json").write_text(json.dumps({"dependencies": {"left-pad": "1.3.0"}}))
        (self.b / "environment/deps/requirements.txt").write_text("fastapi==0.110.0\n")
        self.chain("node", post_install=[
            "npm install --prefix /opt/deps --no-audit --save-exact next@15.5.18 @types/node@22.19.19 eslint@^9",
            "/opt/deps/venv/bin/pip install --no-cache-dir uvicorn==0.30.1"])
        found = {f["subject"]: f["detail"] for f in shipcheck.deps_declared(self.b)}
        self.assertEqual(sorted(found), ["package.json"])      # uvicorn pinned by post_install
        self.assertIn("eslint", found["package.json"])          # a range is not installed pinned
        self.assertNotIn("next", found["package.json"])
        self.assertNotIn("@types/node", found["package.json"])

    def test_a_doc_requiring_a_newer_toolchain_is_flagged(self):
        self.chain("go")
        self.repo("btmonit/README.md", "# btmonit\nwritten in Go 1.26+\n\nNeeds Go 1.26 or later.\nGo 1.21+ works.\n")
        found = shipcheck.docs_toolchain(self.b)
        self.assertEqual([f["subject"] for f in found], ["btmonit/README.md:2", "btmonit/README.md:4"])

    def test_outside_paths_are_guarded_when_advertised_and_declared_otherwise(self):
        self.repo("polya/verify_sources.py", "SRC = '/home/user/How_to_Solve_It_transcription.md'\n")
        self.repo("polya/verify_all.sh", "python3 verify_sources.py\n")
        self.repo("build_docx.py", "OUT = '/home/user/_logs_data.json'\n")
        found = {f["subject"]: f for f in shipcheck.fixture_outside_paths(self.b)}
        self.assertEqual(found["polya/verify_sources.py"]["route"], "fixture")
        self.assertEqual(found["build_docx.py"]["route"], "ledger")
        self.ledger([{"path": "/home/user/_logs_data.json", "reason": "host file"},
                     {"path": "How_to_Solve_It_transcription.md", "reason": "copyrighted"}])
        self.assertEqual([f["subject"] for f in shipcheck.fixture_outside_paths(self.b)],
                         ["polya/verify_sources.py"])

    def test_an_advertised_script_needs_the_guard_not_a_declaration(self):
        self.repo("polya/verify_sources.py", "SRC = '/home/user/How_to_Solve_It_transcription.md'\n")
        self.repo("polya/verify_all.sh", "python3 verify_sources.py\n")
        self.ledger([{"path": "/home/user/How_to_Solve_It_transcription.md", "reason": "host_path_never_in_trace"}])
        self.assertEqual([f["route"] for f in shipcheck.fixture_outside_paths(self.b)], ["fixture"])
        (self.b / "authoring/extraction").mkdir(parents=True)
        (self.b / "authoring/extraction/fixture-authored.json").write_text(json.dumps({"files": {
            "polya/verify_sources.py": {"reason": "guard: reports NOT RUN when the transcription is absent"}}}))
        self.assertEqual(shipcheck.fixture_outside_paths(self.b), [])

    def test_the_ledger_declares_the_host_paths_its_fixture_reads(self):
        from pipeline.ledger.faithfulness import host_paths
        self.repo("buchsymd/ajax/ajax_form.php", "<?php\nrequire_once dirname(__DIR__,3).'/zuba.php';\n")
        self.repo("build_docx.py", "OUT = '/home/user/_logs_data.json'\n")
        got = sorted((h["path"], h["read_by"]) for h in host_paths(self.b))
        self.assertEqual(got, [("/home/user/_logs_data.json", "build_docx.py"),
                               ("zuba.php", "buchsymd/ajax/ajax_form.php")])
        self.ledger([{"path": p, "reason": "host_path_never_in_trace"} for p, _ in got])
        self.assertEqual(shipcheck.fixture_outside_paths(self.b), [])

    def test_a_php_include_above_the_repository_is_declared(self):
        self.repo("buchsymd/ajax/ajax_form.php", "<?php\nrequire_once dirname(__DIR__,3).'/zuba.php';\n")
        found = shipcheck.fixture_outside_paths(self.b)
        self.assertEqual([(f["subject"], f["route"]) for f in found], [("buchsymd/ajax/ajax_form.php:2", "ledger")])
        self.repo("buchsymd/ajax/ajax_form.php", "<?php\nrequire_once dirname(__DIR__,1).'/lib.php';\n")
        self.assertEqual(shipcheck.fixture_outside_paths(self.b), [])

    def test_excluded_files_the_fixture_names_must_be_declared(self):
        (self.b / "authoring/extraction").mkdir(parents=True)
        (self.b / "authoring/extraction/extraction.json").write_text(json.dumps({"stats": {"skipped_before": {
            "oversize": 0, "binaries_excluded": [{"path": "nontonlokal/data/media/demo-senja.mp4", "msg_index": 4}]}}}))
        self.repo("nontonlokal/data/library.json", '{"video": "media/demo-senja.mp4"}')
        self.assertEqual([f["check"] for f in shipcheck.ledger_omissions(self.b)], ["ledger.omissions"])
        self.ledger([{"path": "nontonlokal/data/media/demo-senja.mp4", "reason": "excluded_at_extraction:binaries_excluded"}])
        self.assertEqual(shipcheck.ledger_omissions(self.b), [])

    def test_level_name_and_folder_agree_with_the_ledger(self):
        (self.b / "task.toml").write_text('[task]\nname = "arena/a_f2_x"\n')
        self.assertEqual(shipcheck.level_name(self.b), [])
        self.ledger([], level="F0")
        self.assertEqual(len(shipcheck.level_name(self.b)), 2)


class RunDetectors(Bundle):
    def trial(self, model, n, passed):
        return {"model": model, "trial": f"t{n}", "step": "01", "capture": self.root / "none",
                "grade": {"checks": {"g_keep": {"passed": passed}, "o_fix": {"passed": True}}}}

    def test_a_guardrail_most_episodes_fail_needs_adjudication(self):
        (self.b / "tests").mkdir()
        (self.b / "tests/checks.json").write_text(json.dumps({"checks": [
            {"id": "g_keep", "kind": "guardrail"}, {"id": "o_fix", "kind": "objective"}]}))
        trials = [self.trial("fable", i, i == 0) for i in range(10)] + [self.trial("astra", i, True) for i in range(5)]
        found = shipcheck.runs_guardrail_share(self.b, trials)
        self.assertEqual([f["subject"] for f in found], ["fable/g_keep"])
        self.assertGreater(9 / 10, policy.SHIP_GUARDRAIL_FAIL_SHARE_MAX)

    def test_a_certificate_must_cover_the_shipped_campaigns(self):
        (self.b / "verification").mkdir()
        (self.b / "verification/genuine.json").write_text(json.dumps(
            {"certified": True, "episodes": [{"id": "t0"}, {"id": "t1"}]}))
        trials = [self.trial("fable", i, True) for i in range(3)]
        found = shipcheck.runs_certified(self.b, trials)
        self.assertEqual(found[0]["evidence"], ["t2"])

    def test_waivers_are_human_only(self):
        (self.b / "verification").mkdir()
        waiver = {"check": "image.login_shell", "subject": "python", "reason": "ruled benign"}
        (self.b / shipcheck.WAIVERS).write_text(json.dumps({"adjudicator": "model", "waivers": [waiver]}))
        self.assertEqual(shipcheck.waivers(self.b), [])
        (self.b / shipcheck.WAIVERS).write_text(json.dumps({"adjudicator": "human", "waivers": [waiver]}))
        self.assertEqual(shipcheck.waivers(self.b), [waiver])


class ImageProbe(unittest.TestCase):
    def test_parity_and_quiet_start_come_from_the_probe_text(self):
        text = ("WHICH|sh -c|python|/opt/deps/venv/bin/python\nWHICH|sh -lc|python|/usr/local/bin/python\n"
                "WHICH|sh -c|node|/usr/local/bin/node\nWHICH|sh -lc|node|/usr/local/bin/node\n"
                "QUIET|sh -c|python3|\nQUIET|sh -lc|python3|Error processing line 1 of zz.pth\n")
        found = shipcheck.image_findings(shipcheck.parse_image_probe(text))
        self.assertEqual([(f["check"], f["subject"]) for f in found],
                         [("image.shell_parity", "python"), ("image.quiet_start", "python3")])


class ImportProbe(unittest.TestCase):
    def test_unresolved_imports_and_broken_packages_are_findings(self):
        text = ("WHICH|sh -c|python|/opt/deps/venv/bin/python\nWHICH|sh -lc|python|/opt/deps/venv/bin/python\n"
                "PYIMPORT|pytest|legal_os/backend/tests/test_billing.py\nNODEIMPORT|react|ws-playground/web/src/main.tsx\n"
                "GOIMPORT|cli|cannot find module providing package x\nCOMPOSER|monolog/monolog|app\n"
                "PIPCHECK|fastapi 0.110.0 has requirement starlette<0.37, but you have starlette 0.38.\n")
        found = shipcheck.image_findings(shipcheck.parse_image_probe(text))
        self.assertEqual(sorted(f["subject"] for f in found),
                         ["composer:monolog/monolog", "go:cli", "node:react", "pip check", "py:pytest"])
        self.assertTrue(all(f["route"] == "fixture" and f["measure"] == "screen" for f in found))

    def test_the_probe_script_embeds_both_scanners(self):
        from pipeline import image_probe
        text = image_probe.script()
        self.assertNotIn("@PY_IMPORTS@", text)
        self.assertNotIn("@NODE_IMPORTS@", text)
        compile(image_probe.PY_IMPORTS, "py_imports", "exec")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_a_require_the_code_catches_is_optional(self):
        from pipeline import image_probe
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, True)
        (repo / "index.js").write_text(
            "const express = require('express');\n"
            "function loadCanvas() {\n  try { return require('canvas'); } catch (e) { return null; }\n}\n"
            "try {\n  if (x) { const sharp = require('sharp'); }\n} catch (e) {}\n"
            "if (ok) { require('left-pad'); }\n")
        script = image_probe.NODE_IMPORTS.replace('"/app/repo"', json.dumps(str(repo)))
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
        self.assertEqual(sorted(l.split("|")[1] for l in out.splitlines()), ["express", "left-pad", "sharp"])


class CoverageCritic(unittest.TestCase):
    def test_proposals_are_parsed_from_the_last_json_block(self):
        text = 'thinking\n```json\n{"proposals": [{"clause": "c", "edits": [{"path": "a.js", "old": "x", "new": "y"}]},' \
               ' {"clause": "no edits"}]}\n```'
        self.assertEqual([p["clause"] for p in coverage_critic.parse_proposals(text)], ["c"])
        self.assertEqual(coverage_critic.parse_proposals("no json here"), [])

    def test_an_edit_must_be_exact_and_unique(self):
        tree = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tree, True)
        (tree / "server.js").write_text("const SKIP = [];\nconst SKIP2 = [];\n")
        files = coverage_critic.apply_edits(tree, [{"path": "server.js", "old": "SKIP = []",
                                                    "new": "SKIP = ['node_modules', '.git']"}])
        self.assertIn("'node_modules'", files["server.js"])
        for bad in ({"path": "server.js", "old": "[]", "new": "x"}, {"path": "../etc/passwd", "old": "a", "new": "b"},
                    {"path": "missing.js", "old": "a", "new": "b"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                coverage_critic.apply_edits(tree, [bad])

    def test_patch_paths_name_the_files_a_fix_touches(self):
        patch = "--- a/x/server.js\n+++ b/x/server.js\n@@\n--- a/old.js\n+++ /dev/null\n"
        self.assertEqual(coverage_critic._patch_paths(patch), ["x/server.js"])

    def test_only_an_executed_full_score_is_a_finding(self):
        result = {"gaps": [{"step": "01", "index": 2, "clause": "whichever subfolder", "violation": "skips .git",
                            "edits": []}]}
        found = coverage_critic.findings(result)
        self.assertEqual([(f["check"], f["route"], f["measure"]) for f in found], [("coverage.gap", "hidden", "regrade")])


def setUpModule():
    # These tests pin the gap machinery in its blocking setting; the advisory default since 2026-10-21 ~10:05 IST
    # (policy.LENIENCY_FINDINGS_BLOCK = False) is tested in test_leniency_advisory.py.
    from pipeline import policy
    global _LENIENCY
    _LENIENCY, policy.LENIENCY_FINDINGS_BLOCK = policy.LENIENCY_FINDINGS_BLOCK, True


def tearDownModule():
    from pipeline import policy
    policy.LENIENCY_FINDINGS_BLOCK = _LENIENCY


if __name__ == "__main__":
    unittest.main()


class BuildTimeReadiness(Bundle):
    """The ship-time classes are build blockers the designer can fix (user, 2026-10-07)."""

    def test_findings_become_repair_blockers_with_an_editable_where(self):
        from unittest import mock
        from pipeline import orchestrate
        self.repo("legal_os/backend/requirements.txt", "pytest==7.4.2\n")
        self.repo("btmonit/README.md", "written in Go 1.26+\n")
        (self.b / "tests").mkdir()
        (self.b / "tests/checks.json").write_text("{}")
        gap = {"gaps": [{"step": "01", "index": 3, "clause": "whichever subfolder", "violation": "skips .git",
                         "edits": []}]}
        with mock.patch("pipeline.author.sandbox.available", return_value=(False, "no docker")), \
             mock.patch("pipeline.credential.resolve_credential", return_value="token"), \
             mock.patch("pipeline.coverage_critic.critic", return_value=gap) as critic:
            blockers = orchestrate._ship_readiness(self.b)
            self.assertEqual(critic.call_count, 1)
        by = {b["code"]: b for b in blockers}
        self.assertEqual(by["deps.declared"]["where"], "authoring/spec.json")
        self.assertEqual(by["coverage.gap"]["where"], "tests/suites")
        self.assertIn("determinacy_probe", by["coverage.gap"]["message"])

    def test_the_critic_runs_once_per_build_and_waivers_hold(self):
        from unittest import mock
        from pipeline import coverage_critic, orchestrate
        self.repo("legal_os/backend/requirements.txt", "pytest==7.4.2\n")
        (self.b / "verification").mkdir()
        (self.b / coverage_critic.RECEIPT).write_text("{}")
        (self.b / shipcheck.WAIVERS).write_text(json.dumps({"adjudicator": "human", "waivers": [
            {"check": "deps.declared", "subject": "legal_os/backend/requirements.txt", "reason": "ruled"}]}))
        with mock.patch("pipeline.author.sandbox.available", return_value=(False, "no docker")), \
             mock.patch("pipeline.coverage_critic.critic") as critic:
            self.assertEqual(orchestrate._ship_readiness(self.b), [])
            critic.assert_not_called()


class GuardrailShareInTheCertificate(Bundle):
    def test_a_guardrail_most_episodes_fail_blocks_certification_until_waived(self):
        from pipeline import genuine
        (self.b / "tests").mkdir()
        (self.b / "tests/checks.json").write_text(json.dumps({"checks": [
            {"id": "g_existing_behaviour", "kind": "guardrail"}, {"id": "o_fix", "kind": "objective"}]}))
        rows = [{"model": "anthropic/claude-fable-5-1", "verdict": "capability",
                 "failures": [{"check": "g_existing_behaviour"}]} for _ in range(10)]
        rows += [{"model": "openai/gpt-6-astra", "verdict": "pass", "failures": []} for _ in range(5)]
        problems = genuine.guardrail_share_problems(self.b, rows)
        self.assertEqual(len(problems), 1)
        self.assertIn("claude-fable-5-1/g_existing_behaviour", problems[0])
        (self.b / "verification").mkdir()
        (self.b / shipcheck.WAIVERS).write_text(json.dumps({"adjudicator": "human", "waivers": [
            {"check": "runs.guardrail_share", "subject": "claude-fable-5-1/g_existing_behaviour", "reason": "ruled"}]}))
        self.assertEqual(genuine.guardrail_share_problems(self.b, rows), [])


class ProvenanceIgnoresWhatAFrozenCopyCannotRecompute(unittest.TestCase):
    """2026-10-07: 8 semantic reviews refused to start because the review's frozen
    snapshot has no authoring/extraction/extraction.json, so extraction-declared
    omissions could not be recomputed. Every other measured field still counts."""

    def test_only_extraction_declarations_are_exempt(self):
        from unittest import mock
        from pipeline import release
        stored = {"schema": "arena-pipeline/ledger/4", "source": {"session_id": "s", "seed_index": 0},
                  "entries": [], "recovery_omissions": [
                      {"path": "a.mp4", "reason": "excluded_at_extraction:binaries_excluded"},
                      {"path": "/home/user/x.json", "reason": "host_path_never_in_trace", "read_by": "b.py"}]}
        frozen = {**stored, "recovery_omissions": [stored["recovery_omissions"][1]]}
        root = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, True)
        (root / "provenance").mkdir(); (root / "authoring").mkdir()
        (root / "provenance/ledger.json").write_text(json.dumps(stored))
        (root / "authoring/spec.json").write_text(json.dumps({"source": {"session_id": "s", "seed_index": 0}}))
        with mock.patch("pipeline.ledger.faithfulness.build", return_value=frozen), \
             mock.patch("pipeline.ledger.faithfulness.citation_problems", return_value=[]), \
             mock.patch("pipeline.dump.recover.load_trace", return_value={}):
            self.assertEqual(release._provenance_problems(root), [])
        tampered = {**frozen, "recovery_omissions": []}          # a host path is still compared
        with mock.patch("pipeline.ledger.faithfulness.build", return_value=tampered), \
             mock.patch("pipeline.ledger.faithfulness.citation_problems", return_value=[]), \
             mock.patch("pipeline.dump.recover.load_trace", return_value={}):
            self.assertTrue(release._provenance_problems(root))
