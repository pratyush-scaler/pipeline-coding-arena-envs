"""Every counted failure certified as the model's; hidden-only hardening in minutes.

User, 2026-10-04: "harden tasks in minutes rather than in hours" and "ensure all
the failures are genuine model incapability failures. And there should not be
any grader/env/infra issues."
"""
import tempfile
import unittest
from pathlib import Path

from pipeline import fastharden, genuine
from pipeline.author import determinacy

SUITES = {"test_logcheck.py", "harness.js", "test_no_content.test.js"}


def record(stdout="", stderr="", exit_code=1, **extra):
    return {"passed": False, "exit_code": exit_code, "stdout": stdout, "stderr": stderr,
            "timed_out": False, **extra}


class Classify(unittest.TestCase):
    NO_REPORT = ("runner produced no trustworthy report of completed tests for this selection; "
                 "exit status alone is not evidence")

    def test_a_runner_reported_assertion_is_the_models_failure(self):
        out = ('...status": "failed",\n "error": {\n "message": "Error: Snake Run: [\\"Kart\\"]\\n'
               'expect(received).toBe(expected) // Object.is equality"')
        self.assertEqual(genuine.classify(record(out, error=self.NO_REPORT), SUITES)[0], "assertion")

    def test_a_matcher_failure_stands_when_the_status_key_was_cut(self):
        out = (' "line": 20\n },\n "message": "Error: [\\"Kart\\"]\\n\\u001b[2mexpect(\\u001b[22m\\u001b[31mreceived'
               '\\u001b[39m\\u001b[2m).\\u001b[22mtoEqual\\u001b[2m(\\u001b[22m\\u001b[32mexpected\\u001b[39m) // deep equality"')
        self.assertEqual(genuine.classify(record(out, error=self.NO_REPORT), SUITES)[0], "assertion")
        # The real message runs on into a long diff before its closing quote.
        long = out[:-1] + "\\n- Expected  - 2" + " x" * 300 + '"'
        self.assertEqual(genuine.classify(record(long, error=self.NO_REPORT), SUITES)[0], "assertion")

    def test_an_unreported_or_non_assertion_failure_stays_suspect(self):
        for out in ("", '"status": "passed", "message": "expect(x).toBe(y)"',
                    '"status": "failed", "message": "page.goto: net::ERR_CONNECTION_REFUSED"'):
            self.assertEqual(genuine.classify(record(out, error=self.NO_REPORT), SUITES)[0], "suspect", out)

    def test_a_pytest_assertion_on_behaviour(self):
        out = ("    def test_hyphen_typed_as_dash():\n>       assert run(compilation(paras)) == expected\n"
               "E       AssertionError: assert {1: ['dash']} == {1: ['dash'], 12: ['dash']}\n\n"
               "test_logcheck.py:222: AssertionError\nFAILED test_logcheck.py::test_hyphen_typed_as_dash\n")
        self.assertEqual(genuine.classify(record(out), SUITES)[0], "assertion")

    def test_a_node_assertion(self):
        out = ("AssertionError [ERR_ASSERTION]: CBSE class 7 Science has no content\n"
               "    at TestContext.<anonymous> (/tests/suites/test_no_content.test.js:17:12)\n"
               "    at Test.run (node:internal/test_runner/test:1047:25)\n")
        self.assertEqual(genuine.classify(record(out), SUITES)[0], "assertion")

    def test_the_submission_crashing_is_its_own_failure(self):
        out = ('Traceback (most recent call last):\n  File "/tests/suites/test_logcheck.py", line 40, in test_x\n'
               '    check(text)\n  File "/tmp/work-1/repo/logcheck.py", line 12, in check\n'
               '    return parts[3]\nE   IndexError: list index out of range\n'
               "/tmp/work-1/repo/logcheck.py:12: IndexError\n")
        kind, detail = genuine.classify(record(out), SUITES)
        self.assertEqual(kind, "repo_crash")
        self.assertIn("logcheck.py", detail)

    def test_ours_is_never_counted(self):
        cases = [
            ({"passed": False, "exit_code": None, "error": "grader: code the hidden tests would import reaches ..."},
             "grader refused"),
            (record(timed_out=True), "timed out"),
            (record(exit_code=None, reason="exit None"), "no exit status"),
            (record(""), "no output"),
            (record("INTERNALERROR> boom"), "completed no test"),
            (record("ERROR test_logcheck.py - fixture 'tmp_path' not found\nerrors during collection\n"),
             "collection"),
            (record('  File "/tests/suites/test_logcheck.py", line 3, in <module>\n'
                    "    import yaml\nModuleNotFoundError: No module named 'yaml'\n"), ""),
        ]
        for rec, why in cases:
            with self.subTest(why=why):
                kind, detail = genuine.classify(rec, SUITES)
                self.assertIn(kind, ("suspect", "interface"))
                if why:
                    self.assertIn(why, detail)

    def test_a_test_reading_malformed_output_is_output(self):
        out = ('  File "/tests/suites/test_logcheck.py", line 50, in test_report\n'
               '    assert result["dash"] == 3\nE   KeyError: \'dash\'\n/tests/suites/test_logcheck.py:50: KeyError\n')
        self.assertEqual(genuine.classify(record(out), SUITES)[0], "output")


class HiddenOnly(unittest.TestCase):
    before = {"steps": [{"name": "01", "instruction": "fix it"}], "toolchain": {"name": "python"},
              "deps": {"requirements.txt": "x==1\n"}, "deliverables": [], "suites": {"t.py": "a"}}

    def test_hidden_changes_are_allowed(self):
        after = dict(self.before, suites={"t.py": "a", "u.py": "b"}, checks=[{"id": "o_new"}])
        self.assertEqual(fastharden.hidden_only_problems(self.before, after), [])

    def test_anything_the_agent_sees_is_refused(self):
        for key, value in (("steps", [{"name": "01", "instruction": "fix it, carefully"}]),
                           ("toolchain", {"name": "node"}), ("deps", {"requirements.txt": "x==2\n"}),
                           ("deliverables", [{"path": "out.json", "kind": "json", "step": "01"}])):
            with self.subTest(key=key):
                self.assertTrue(fastharden.hidden_only_problems(self.before, dict(self.before, **{key: value})))

    def test_the_built_environment_is_compared_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            for root, text in ((a, "x = 1\n"), (b, "x = 2\n")):
                (root / "environment/repo").mkdir(parents=True)
                (root / "environment/repo/m.py").write_text(text)
            problems = fastharden.hidden_only_problems(self.before, self.before, a, b)
            self.assertTrue(any("environment" in p for p in problems))


class Determinacy(unittest.TestCase):
    def test_every_family_must_give_the_reference_answer(self):
        def ask(model, prompt):
            return "The rule names any hyphen.\nANSWER: Yes." if "fable" in model else "ANSWER: yes"
        result = determinacy.probe("no dashes", "a U+2011 between spaces", "dash?", "yes", ask=ask)
        self.assertTrue(result["determinate"])

    def test_one_dissent_or_undetermined_drops_the_case(self):
        for other in ("ANSWER: no", "ANSWER: UNDETERMINED", "no final line"):
            def ask(model, prompt, other=other):
                return "ANSWER: yes" if "fable" in model else other
            with self.subTest(other=other):
                self.assertFalse(determinacy.probe("r", "c", "q", "yes", ask=ask)["determinate"])

    def test_a_failed_call_admits_nothing(self):
        def ask(model, prompt):
            raise RuntimeError("rate limited")
        result = determinacy.probe("r", "c", "q", "yes", ask=ask)
        self.assertFalse(result["determinate"])
        self.assertTrue(all("error" in a for a in result["answers"].values()))

    def test_the_prompt_shows_the_instruction_and_never_the_expected_answer(self):
        seen = []

        def ask(model, prompt):
            seen.append(prompt)
            return "ANSWER: yes"
        determinacy.probe("THE-INSTRUCTION", "THE-CASE", "THE-QUESTION", "SECRET-EXPECTED", ask=ask)
        self.assertIn("THE-INSTRUCTION", seen[0])
        self.assertNotIn("SECRET-EXPECTED", seen[0])


class FastBrief(unittest.TestCase):
    def test_the_brief_freezes_the_agent_surface_and_demands_genuine_cases(self):
        for phrase in ("FROZEN", "determinacy_probe", "IN-SAMPLE", "regrade_episodes", "rule_quote"):
            self.assertIn(phrase, fastharden.FAST_BRIEF)

    def test_the_fast_tools_exist_only_for_captured_sessions(self):
        from pipeline.author import agent_designer
        names = {t["name"] for t in agent_designer.FAST_TOOLS}
        self.assertEqual(names, {"regrade_episodes", "read_episode", "coverage_critic"})
        self.assertFalse(names & {t["name"] for t in agent_designer._tools(0)})
        # the determinacy probe serves every design since the baseline difficulty loop (2026-10-04)
        self.assertIn("determinacy_probe", {t["name"] for t in agent_designer._tools(0)})


class LeverCatalogue(unittest.TestCase):
    def test_the_astra_levers_are_catalogued_and_both_directions_reach_the_opening(self):
        from pipeline.author import levers
        for name in ("unicode_class_member", "field_not_line", "first_green_stop", "class_member_x_overreach"):
            self.assertIn(name, levers.LEVERS)
        self.assertIn("class_member_x_overreach", levers.INTERLOCK_RECIPES)
        self.assertIn("BROADER", levers.relevant_text({}, None))


if __name__ == "__main__":
    unittest.main()


class ScannerNarrowing(unittest.TestCase):
    """User, 2026-10-04: own-process kills, /tmp scratch symlinks and repo-test globs are benign;
    anything reaching /tests, the grader, reward files or graded paths stays flagged."""
    known = frozenset({"/app/repo"})

    def tampers(self, command):
        from pipeline import attribution as a
        return a._tampers(command, self.known) and not all(
            a._scratch_only_customize(p) and not a._TAMPER_RE.search(a._CUSTOMIZE.sub(" ", a._joined(p)))
            for p in a._readings(command, self.known))

    def test_own_process_kills_and_scratch_customize_are_benign(self):
        self.assertFalse(self.tampers('cd /app/repo/x && pkill -f "node server.js"; (node server.js &)'))
        self.assertFalse(self.tampers("cd /tmp/t1 && mkdir -p n && cat > n/sitecustomize.py <<'EOF'\nimport sys\nEOF"))

    def test_harness_tampering_stays_flagged(self):
        for command in ("pkill -f pytest", "killall harbor", "pkill -9 -1", "trap 'exit 0' EXIT",
                        "cat > /app/repo/sitecustomize.py <<'EOF'\nimport os\nEOF"):
            with self.subTest(command=command):
                self.assertTrue(self.tampers(command))

    def test_repo_test_globs_are_not_test_access_but_slash_tests_is(self):
        from pipeline import attribution as a

        def access(command):
            return any(a._GRADED_RE.search(a._without_relative_globs(v))
                       for p in a._readings(command, self.known) for v in (a._joined(p), a._rooted(p)))
        self.assertFalse(access("find . -name __pycache__ -path '*/tests/*' -prune"))
        self.assertTrue(access("cat /tests/test_x.py"))
        self.assertTrue(access("find / -path '*/tests/*'"))

    def test_symlinks_inside_scratch_are_benign_and_into_the_repo_are_not(self):
        from pipeline import attribution as a

        def flagged(command):
            return bool(a._SYMLINK_RE.search(command)) and not all(
                a._scratch_symlink(command, p) for p in a._readings(command, self.known))
        self.assertFalse(flagged("mkdir -p /tmp/t/p && ln -sf /app/repo/x.php /tmp/t/p/x.php\ncat > /tmp/t/z.php <<'EOF'\n<?php $a = new PDO('sq\nEOF"))
        self.assertTrue(flagged("ln -s /tests /app/repo/t"))

    def test_only_a_human_adjudication_is_honoured(self):
        import hashlib
        import json
        from pipeline import attribution as a
        hit = {"trial": "t1", "step": "01", "kind": "tamper", "evidence": "turn 3: pkill -f pytest"}
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            (bundle / "verification").mkdir()
            flag = {"trial": "t1", "step": "01", "kind": "tamper", "verdict": "benign",
                    "evidence_sha256": hashlib.sha256(hit["evidence"].encode()).hexdigest()}
            for adjudicator, expected in (("model", False), ("human", True)):
                (bundle / "verification/hack-adjudications.json").write_text(
                    json.dumps({"adjudicator": adjudicator, "flags": [flag]}))
                self.assertEqual(a._adjudication_key(hit) in a.adjudications(bundle), expected)


class GoFailures(unittest.TestCase):
    go = "a failed or skipped private Go test is not a pass"

    def test_go_assertion_and_panics(self):
        rec = {"passed": False, "exit_code": 1, "error": self.go,
               "stdout": "    battery_test.go:42: Charging = false, want true\n--- FAIL: TestCharging (0.00s)\n"}
        self.assertEqual(genuine.classify(rec, set())[0], "assertion")
        crash = dict(rec, stdout="--- FAIL: T\npanic: index out of range\n\ngoroutine 7 [running]:\n"
                                 "\t/tmp/w/repo/battery/parse.go:57 +0x1d\n\t/tests/suites/battery_test.go:30 +0x55\n")
        self.assertEqual(genuine.classify(crash, {"battery_test.go"})[0], "repo_crash")
        ours = dict(rec, stdout="--- FAIL: T\npanic: boom\n\ngoroutine 7 [running]:\n\t/tests/suites/battery_test.go:30\n")
        self.assertEqual(genuine.classify(ours, {"battery_test.go"})[0], "suspect")
        skipped = dict(rec, stdout="--- SKIP: TestCharging (0.00s)\n")
        self.assertEqual(genuine.classify(skipped, set())[0], "suspect")


class BaselineDifficultyLoop(unittest.TestCase):
    """User, 2026-10-04: meet pass@1 <= 0.5 at baseline creation; hardening is the backstop."""

    def test_the_loop_reaches_every_first_design(self):
        from pipeline import policy
        from pipeline.author import agent_designer as a
        self.assertIn("THE DIFFICULTY LOOP", a.AUTHORING_MANDATE)
        self.assertIn("determinacy_probe", a.DIFFICULTY_LOOP)
        self.assertGreaterEqual(a._blind_budget(), policy.BASELINE_BLIND_SOLVES)
        spec = next(t for t in a._tools(0) if t["name"] == "blind_solve")
        self.assertEqual(set(spec["input_schema"]["properties"]["model"]["enum"]), {"astra", "fable"})

    def test_families_map_to_the_calibration_models(self):
        from pipeline import policy
        from pipeline.author import agent_designer as a
        self.assertEqual(set(a.BLIND_FAMILIES.values()), set(policy.resolve_calibration_models()))
        self.assertEqual(a._family_of("gpt-6-astra"), "astra")
        self.assertEqual(a._family_of("anthropic/claude-fable-5-1"), "fable")


class CodexSlots(unittest.TestCase):
    def test_slots_are_shared_across_holders_and_released(self):
        from unittest.mock import patch
        from pipeline.benchmark import capacity
        with tempfile.TemporaryDirectory() as tmp, patch.object(capacity, "CODEX_SLOTS_DIR", tmp), \
                patch.dict("os.environ", {"ARENA_CODEX_SLOTS": "3"}):
            with capacity.codex_slots("gpt-6-astra", 5) as first:
                self.assertEqual(first, 3)                   # capped at the machine-wide pool
                import threading
                got = []
                t = threading.Thread(target=lambda: got.append(
                    capacity.codex_slots("gpt-6-astra", 2, poll_s=0.05).__enter__()))
                t.start(); t.join(0.3)
                self.assertEqual(got, [])                     # waits while every slot is held
            t.join(2)
            self.assertEqual(got, [2])                        # released on exit
            with capacity.codex_slots("claude-fable-5-1", 5) as fable:
                self.assertEqual(fable, 5)                    # Fable is never limited

    def test_unset_means_unlimited(self):
        from unittest.mock import patch
        from pipeline.benchmark import capacity
        with patch.dict("os.environ", {"ARENA_CODEX_SLOTS": ""}):
            with capacity.codex_slots("gpt-6-astra", 5) as n:
                self.assertEqual(n, 5)


class GuardrailRewording(unittest.TestCase):
    """2026-10-04: five lineages stopped for a reworded or strengthened guardrail."""
    from pipeline import harden as h
    old = {"id": "g_shadow", "kind": "guardrail", "weight": 0, "step": "01", "basis": "x",
           "runner": {"type": "shadow-guard", "modules": ["pytest", "csv"]}}

    def problems(self, new):
        before = {"checks": [self.old], "steps": [{"name": "01"}]}
        after = {"checks": [new], "steps": [{"name": "01"}]}
        from pipeline import harden
        return harden.revision_problems(before, after, [])

    def test_rewording_and_strengthening_are_allowed(self):
        self.assertEqual(self.problems(dict(self.old, basis="reworded", rule_quote=["new words"])), [])
        stronger = dict(self.old, runner={"type": "shadow-guard", "modules": ["pytest", "csv", "pandas"]})
        self.assertEqual(self.problems(stronger), [])

    def test_weakening_retyping_and_reweighting_are_refused(self):
        for new in (dict(self.old, runner={"type": "shadow-guard", "modules": ["pytest"]}),
                    dict(self.old, runner={"type": "hash-guard", "paths": ["a"]}),
                    dict(self.old, weight=3), dict(self.old, kind="objective")):
            with self.subTest(new=new):
                self.assertTrue(any("untargeted" in p for p in self.problems(new)))


class NeverStartedTrials(unittest.TestCase):
    def test_only_a_trial_with_no_agent_output_and_an_exception_counts(self):
        import json
        from pipeline.evidence import never_started
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "result.json").write_text(json.dumps({"exception_info": {
                "exception_type": "RuntimeError", "exception_message": "all predefined address pools have been fully subnetted"}}))
            self.assertTrue(never_started(t))
            (t / "steps/01/agent").mkdir(parents=True)
            (t / "steps/01/agent/trajectory.json").write_text("{}")
            self.assertFalse(never_started(t))                 # an agent ran: its isolation proof is required
            (t / "steps/01/agent/trajectory.json").unlink()
            (t / "result.json").write_text(json.dumps({"exception_info": None}))
            self.assertFalse(never_started(t))                 # no exception: not a start failure


class TrialSlots(unittest.TestCase):
    def test_every_model_shares_the_trial_pool(self):
        from unittest.mock import patch
        from pipeline.benchmark import capacity
        with tempfile.TemporaryDirectory() as tmp, patch.object(capacity, "TRIAL_SLOTS_DIR", tmp), \
                patch.dict("os.environ", {"ARENA_TRIAL_SLOTS": "4"}):
            with capacity.trial_slots(3) as a, capacity.trial_slots(5) as b:
                self.assertEqual((a, b), (3, 1))
