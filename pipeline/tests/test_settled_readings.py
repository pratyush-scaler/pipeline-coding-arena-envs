"""Unsettled readings: the failures a run asked about, and decisive checks the instruction does not settle.

User, 2026-10-21, after the external audit of the shipped Batch 3: c-game-bot (a_f2_c-game-bot) put 41 points on a
physics replay its instruction never states. Astra run package__gMGGfLM asked "For BOGUS, should I also verify position
and velocity changes against the harness's physics equations?" through `request_user_input_async`, got no answer,
failed o_crash_ledge, o_clip, o_bogus_dry and o_bogus_after, and was certified `capability`.

The fixture under fixtures/c_game_bot is lifted from that package: its instruction, checks.json, test_machine.py (kept
as .txt so this directory never collects it), the spec's checks (rule_quote), and the run's own trajectory and grade
record. Every model call is mocked.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import attribution, genuine, policy
from pipeline.artifacts import atomic_json

FIXTURE = Path(__file__).parent / "fixtures/c_game_bot"
STEP = "01-audit-the-recorded-runs"
ASTRA, FABLE = "openai/gpt-6-astra", "anthropic/claude-fable-5-1"
REPLAYED = ("o_bogus_after", "o_bogus_dry", "o_clip", "o_crash_ledge")
QUESTION = ("For BOGUS, should I also verify position and velocity changes against the harness’s physics equations? "
            "Your stated checks cover controls, fuel, and the map’s starting state.")
#: The sentence a judge that calls the point settled quotes (verbatim from the shipped instruction).
DECIDING = "if any line of a recording could not have come from the line before it, the file is junk"


def _bundle(root: Path) -> Path:
    bundle = root / "v6"
    (bundle / "steps" / STEP).mkdir(parents=True)
    (bundle / "task.toml").write_text(f'[[steps]]\nname = "{STEP}"\n')
    shutil.copyfile(FIXTURE / "instruction.md", bundle / "steps" / STEP / "instruction.md")
    (bundle / "tests/suites").mkdir(parents=True)
    shutil.copyfile(FIXTURE / "checks.json", bundle / "tests/checks.json")
    shutil.copyfile(FIXTURE / "test_machine.py.txt", bundle / "tests/suites/test_machine.py")
    (bundle / "authoring").mkdir()
    shutil.copyfile(FIXTURE / "spec.json", bundle / "authoring/spec.json")
    (bundle / "environment/repo/mars-lander-test").mkdir(parents=True)
    (bundle / "environment/repo/mars-lander-test/audit.py").write_text("def audit_run(m, t):\n    return {}\n")
    flags = {STEP: {c["id"]: True for c in json.loads((FIXTURE / "checks.json").read_text())["checks"]}}
    atomic_json(bundle / "verification/controls.json", {"results": {
        "golden": {"valid": True, "reward": 1.0, "flags": flags},
        "alternative": {"valid": True, "reward": 1.0, "flags": flags}}})
    return bundle


def _episode_dir(root: Path, trial: str, trajectory: dict | Path, grade: dict | Path) -> Path:
    folder = root / "runs" / trial / "steps" / STEP
    (folder / "agent").mkdir(parents=True)
    (folder / "verifier").mkdir(parents=True)
    for target, source in ((folder / "agent/trajectory.json", trajectory), (folder / "verifier/arena-grade.json", grade)):
        if isinstance(source, Path):
            shutil.copyfile(source, target)
        else:
            target.write_text(json.dumps(source))
    capture = folder / "artifacts/app/repo/mars-lander-test"
    capture.mkdir(parents=True)
    (capture / "audit.py").write_text("def audit_run(m, t):\n    return {'verdict': 'BOGUS'}\n")
    return folder


def _quiet_run() -> dict:
    """A run that asks nothing and states no reading: progress notes only."""
    return {"schema_version": "ATIF-v1.7", "steps": [
        {"step_id": 5, "source": "user", "message": "make audit.py do the job properly."},
        {"step_id": 6, "source": "agent", "message": "Reading the auditor and the map format first.",
         "tool_calls": [{"function_name": "exec", "arguments": {"input": "text(await tools.exec_command({cmd:\"cat audit.py\"}))"}}]},
        {"step_id": 7, "source": "agent", "message": "Rewrote landing_zone to walk the points in file order. All tests pass."}]}


def _failed_zone_grade() -> dict:
    out = ("F                                                                        [100%]\n"
           "=================================== FAILURES ===================================\n"
           "___________________________ test_landing_zone_runs ____________________________\n"
           "E       AssertionError: assert (1000, 2000, 150) == (500, 2000, 150)\n"
           "test_audit.py:41: AssertionError\nFAILED test_audit.py::test_landing_zone_runs - AssertionError\n"
           "1 failed in 0.21s\n")
    return {"schema": "arena-coding/grade/1", "step": STEP, "reward": 0.89, "checks": {
        "o_zone_run": {"kind": "objective", "weight": 11, "passed": False, "gated_out": False, "reason": "exit 1",
                       "exit_code": 1, "stdout": out, "stderr": "", "timed_out": False}}}


class Judges:
    """Both calibration families, mocked. Routes on the prompt each module sends."""

    def __init__(self, relevant="YES", settled="UNDETERMINED", quote="NONE", items=None, fail=(),
                 reading="UNDETERMINED", reading_quote="NONE", second="NONE", took="NO"):
        self.relevant, self.settled, self.quote, self.fail = relevant, settled, quote, set(fail)
        self.items = [f"ASKED | {QUESTION}"] if items is None else items
        self.reading, self.reading_quote, self.second, self.took = reading, reading_quote, second, took
        self.calls: list[tuple[str, str]] = []

    def __call__(self, model, prompt, effort="high", max_tokens=0):
        self.calls.append((model, prompt))
        if model in self.fail:
            raise RuntimeError("HTTP 529 overloaded")
        if prompt.startswith("Below are the words a coding agent wrote"):
            return "\n".join([f"ITEM: {i}" for i in self.items] + [f"ITEMS: {len(self.items)}"])
        if "THE FAILED CHECK" in prompt:
            return (f"The run asked whether to replay the motion.\nRELEVANT: {self.relevant}\n"
                    f"SETTLED: {self.settled}\nQUOTE: {self.quote}")
        if "THE HIDDEN TEST SOURCE" in prompt:
            return (f"Reasoning.\nSETTLED: {self.reading}\nQUOTE: {self.reading_quote}\n"
                    f"SECOND_READING: {self.second}\nRUNS_TOOK_IT: {self.took}")
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")

    def asked(self, marker: str) -> int:
        return sum(1 for _, p in self.calls if marker in p)


class GenuineAmbiguity(unittest.TestCase):
    """genuine.audit reads each failing run's own words before it calls a failure the model's."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bundle = _bundle(self.root)
        self.asked_dir = _episode_dir(self.root, "package__gMGGfLM", FIXTURE / "trajectory-gMGGfLM.json",
                                      FIXTURE / "arena-grade-gMGGfLM.json")
        self.episodes = [self._episode(ASTRA, "package__gMGGfLM", self.asked_dir)]

    @staticmethod
    def _episode(model, trial, folder):
        return {"model": model, "campaign": "c", "id": trial, "valid": True, "verdict": "capability",
                "reasons": [], "evidence_problems": [], "steps": [{"name": STEP, "evidence_dir": str(folder),
                                                                   "reward": 0.59}]}

    def audit(self, judges):
        with patch.object(genuine, "episodes", lambda bundle, record=None: [dict(e) for e in self.episodes]):
            return genuine.audit(self.bundle, ask=judges)

    def failures(self, doc, trial="package__gMGGfLM"):
        return {i["check"]: i for e in doc["episodes"] if e["id"] == trial for i in e["failures"]}

    def test_the_tool_question_is_read_from_the_trajectory(self):
        notes = attribution.run_notes(self.asked_dir)
        asked = [n["text"] for n in notes if n["kind"] == "asked"]
        self.assertEqual(asked, [QUESTION + " (options: Check the stated rules only / Also verify position and "
                                            "velocity physics)"])
        found = genuine.detected(notes)
        self.assertEqual(found[0]["found_by"], "ask-the-user tool")
        # The run also wrote down the reading it went with ("... unless you request physics validation too").
        self.assertTrue(any(i["kind"] == "chose" and "unless you request physics validation" in i["text"]
                            for i in found), found)

    def test_gMGGfLM_asked_about_the_physics_and_is_not_certified(self):
        judges = Judges(relevant="YES", settled="UNDETERMINED")
        doc = self.audit(judges)
        self.assertFalse(doc["certified"])
        failures = self.failures(doc)
        self.assertEqual(sorted(failures), sorted(REPLAYED))
        for cid in REPLAYED:
            self.assertEqual(failures[cid]["kind"], "ambiguity_suspect", cid)
            self.assertEqual(failures[cid]["ambiguity"]["status"], "unsettled")
            self.assertEqual(failures[cid]["ambiguity"]["was"], "assertion")
            problem = next(p for p in doc["problems"] if f"package__gMGGfLM {STEP}/{cid}:" in p)
            self.assertIn("ambiguity_suspect", problem)
            self.assertIn("should I also verify position and velocity", problem)      # the run
            self.assertIn("Route to the instruction", problem)
        # ... naming the sentence the check enforces.
        crash = next(p for p in doc["problems"] if "/o_crash_ledge:" in p)
        self.assertIn("it LANDED if that first piece of ground is part of the landing zone", crash)
        self.assertEqual(doc["kinds"], {"ambiguity_suspect": 4})
        receipt = json.loads((self.bundle / genuine.AMBIGUITY_RECEIPT).read_text())
        self.assertEqual(receipt["grading_sha256"], attribution.grading_sha(self.bundle))
        self.assertEqual(doc["grading_sha256"], receipt["grading_sha256"])
        # Both families judged every one of the four failed checks.
        self.assertEqual(judges.asked("THE FAILED CHECK"), 8)

    def test_settled_by_both_families_stays_genuine(self):
        doc = self.audit(Judges(relevant="YES", settled="YES", quote=f'"{DECIDING}."'))
        self.assertTrue(doc["certified"], doc["problems"])
        for item in self.failures(doc).values():
            self.assertEqual(item["kind"], "assertion")
            self.assertEqual(item["ambiguity"]["status"], "settled")
        self.assertEqual(doc["ambiguity"]["settled"], 4)

    def test_a_yes_without_a_sentence_from_the_instruction_settles_nothing(self):
        doc = self.audit(Judges(relevant="YES", settled="YES",
                                quote='"each line must follow from the one before under 3.711 gravity"'))
        self.assertFalse(doc["certified"])
        self.assertTrue(all(i["kind"] == "ambiguity_suspect" for i in self.failures(doc).values()))

    def test_an_erring_family_never_clears_a_failure(self):
        # Fable calls the question unrelated; Astra's judge errs. Unrelated needs EVERY family to say so.
        judges = Judges(relevant="NO", settled="YES", quote=f'"{DECIDING}"')
        original = judges.__call__

        def ask(model, prompt, **kw):
            if "THE FAILED CHECK" in prompt and model.startswith("gpt-"):
                raise RuntimeError("HTTP 500")
            return original(model, prompt, **kw)
        doc = self.audit(ask)
        self.assertFalse(doc["certified"])
        self.assertTrue(all(i["kind"] == "ambiguity_suspect" for i in self.failures(doc).values()))
        self.assertTrue(any("HTTP 500" in p for p in doc["problems"]), doc["problems"])

    def test_a_quiet_run_on_a_settled_check_stays_capability(self):
        quiet = _episode_dir(self.root, "package__quiet", _quiet_run(), _failed_zone_grade())
        self.episodes = [self._episode(FABLE, "package__quiet", quiet)]
        judges = Judges(items=[])
        doc = self.audit(judges)
        self.assertTrue(doc["certified"], doc["problems"])
        item = self.failures(doc, "package__quiet")["o_zone_run"]
        self.assertEqual(item["kind"], "assertion")
        self.assertEqual(item["ambiguity"], {"status": "none"})
        self.assertEqual(judges.asked("THE FAILED CHECK"), 0)      # nothing asked or chosen: nothing to judge

    def test_a_question_unrelated_to_the_failed_check_is_recorded_not_held(self):
        doc = self.audit(Judges(relevant="NO"))
        self.assertTrue(doc["certified"], doc["problems"])
        self.assertEqual({i["ambiguity"]["status"] for i in self.failures(doc).values()}, {"unrelated"})

    def test_judgements_are_asked_once_per_grading_sha(self):
        judges = Judges(relevant="YES", settled="UNDETERMINED")
        self.audit(judges)
        first = len(judges.calls)
        self.audit(judges)
        self.assertEqual(len(judges.calls), first)                  # the receipt answers the second audit
        instruction = self.bundle / "steps" / STEP / "instruction.md"
        spec = json.loads((self.bundle / "authoring/spec.json").read_text())
        spec["steps"][0]["instruction"] += " and the motion has to add up."
        atomic_json(self.bundle / "authoring/spec.json", spec)
        instruction.write_text(spec["steps"][0]["instruction"])
        self.audit(judges)
        self.assertGreater(len(judges.calls), first)                # a new grading surface is judged afresh

    def test_an_erring_judgement_is_asked_again(self):
        self.audit(Judges(relevant="YES", settled="YES", quote=f'"{DECIDING}"', fail={"gpt-6-astra"}))
        healthy = Judges(relevant="YES", settled="YES", quote=f'"{DECIDING}"')
        doc = self.audit(healthy)
        self.assertTrue(doc["certified"], doc["problems"])
        self.assertGreater(healthy.asked("THE FAILED CHECK"), 0)

    def test_a_run_whose_words_cannot_be_read_is_not_capability(self):
        (self.asked_dir / "agent/trajectory.json").unlink()
        doc = self.audit(Judges())
        self.assertFalse(doc["certified"])
        self.assertTrue(any("could not be read" in p for p in doc["problems"]), doc["problems"])

    def test_the_claude_code_transcript_is_read_when_no_trajectory_is_retained(self):
        folder = self.root / "cc"
        (folder / "agent").mkdir(parents=True)
        lines = [{"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "Two ways to read the limits sentence."},
            {"type": "tool_use", "name": "AskUserQuestion", "input": {"questions": [
                {"question": "Should BOGUS also replay the physics?", "header": "Scope",
                 "options": [{"label": "Stated limits only"}, {"label": "Replay physics too"}]}]}},
            {"type": "text", "text": "I'll treat the listed limits as the whole rule."}]}}]
        (folder / "agent/claude-code.txt").write_text("\n".join(json.dumps(line) for line in lines))
        notes = attribution.run_notes(folder)
        self.assertIn({"turn": 1, "kind": "asked",
                       "text": "Should BOGUS also replay the physics? (options: Stated limits only / Replay physics too)"},
                      notes)
        self.assertEqual({i["kind"] for i in genuine.detected(notes)}, {"asked", "chose"})

    def test_an_extracted_item_must_be_the_runs_own_words(self):
        notes = attribution.run_notes(self.asked_dir)
        record = genuine.extract(notes, ask=Judges(items=["CHOSE | I will ignore gravity entirely.",
                                                          f"ASKED | {QUESTION}"]))
        self.assertEqual([i["text"] for i in record["items"]], [QUESTION] * 2)          # one per family
        self.assertEqual(len(record["unverified"]), 2)


def _report(folder: Path, failed=6, passed=4) -> dict:
    return {"model": ASTRA,
            "by_step": {STEP: {"checks": {
                "o_bogus_after": {"passed": passed, "failed": failed, "gated_out": 0, "kind": "objective"},
                "o_zone_run": {"passed": 9, "failed": 1, "gated_out": 0, "kind": "objective"},
                "o_bogus": {"passed": 10, "failed": 0, "gated_out": 0, "kind": "objective"}}}},
            "trials": [{"id": "package__gMGGfLM", "valid": True,
                        "steps": [{"name": STEP, "evidence_dir": str(folder)}]}],
            "rollouts": [{"id": f"package__gMGGfLM/{STEP}", "episode": 1, "step": STEP,
                          "checks": {"o_bogus_after": "failed", "o_zone_run": "passed", "o_bogus": "passed"},
                          "failed_output": {"o_bogus_after": "E   AssertionError: a line taken out of the file"}}]}


class SettledReadingObligation(unittest.TestCase):
    """attribution.reading_problems: every decisive check needs a reading both families call settled."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bundle = _bundle(self.root)
        self.folder = _episode_dir(self.root, "package__gMGGfLM", FIXTURE / "trajectory-gMGGfLM.json",
                                   FIXTURE / "arena-grade-gMGGfLM.json")
        self.report = _report(self.folder)

    def test_the_obligation_is_policy(self):
        self.assertIn("reading_settled", policy.ALL_FAIL_PROOF_OBLIGATIONS)

    def test_decisive_means_a_hard_decision(self):
        self.assertEqual(attribution.decisive_checks(self.bundle, self.report), {(STEP, "o_bogus_after"): 0.6})
        self.assertGreaterEqual(0.6, policy.SIGNAL_HARD_FAIL_RATE)
        self.assertEqual(attribution.decisive_checks(self.bundle, _report(self.folder, failed=2, passed=8)), {})

    def test_an_unsettled_reading_fails_the_obligation(self):
        judges = Judges(reading="UNDETERMINED")
        issues = attribution.reading_problems(self.bundle, self.report, ask=judges)
        self.assertEqual(len(issues), 1, issues)
        self.assertIn(f"{STEP}/o_bogus_after: failed by 60% of gpt-6-astra's valid runs", issues[0])
        self.assertIn("reading_settled", issues[0])
        self.assertIn("state the rule in the user's voice, or remove the assertion", issues[0])
        self.assertIn("test_machine.py::test_a_line_taken_out_of_the_file", issues[0])
        # What the judges were shown: the instruction, the published sentence, the hidden test, the failing run.
        prompt = next(p for _, p in judges.calls if "THE HIDDEN TEST SOURCE" in p)
        self.assertIn("could not have come from the line before it", prompt)
        self.assertIn("def test_a_line_taken_out_of_the_file", prompt)
        self.assertIn("a line taken out of the file", prompt)
        self.assertIn(QUESTION, prompt)
        self.assertIn("+    return {'verdict': 'BOGUS'}", prompt)
        receipt = json.loads((self.bundle / attribution.READINGS).read_text())
        self.assertEqual(receipt["grading_sha256"], attribution.grading_sha(self.bundle))
        entry = receipt["checks"][f"{STEP}/o_bogus_after"]
        self.assertEqual(entry["verdict"], "unsettled")
        self.assertEqual(entry["fail_rate"], {ASTRA: 0.6})

    def test_a_settled_reading_with_its_sentence_passes(self):
        judges = Judges(reading="YES", reading_quote=f'"{DECIDING}: BOGUS"')
        self.assertEqual(attribution.reading_problems(self.bundle, self.report, ask=judges), [])
        self.assertEqual(judges.asked("THE HIDDEN TEST SOURCE"), 2)

    def test_a_second_reading_the_runs_took_fails_it_even_under_yes(self):
        judges = Judges(reading="YES", reading_quote=f'"{DECIDING}"',
                        second="the listed limits are the whole rule; motion is not replayed", took="YES")
        issues = attribution.reading_problems(self.bundle, self.report, ask=judges)
        self.assertEqual(len(issues), 1, issues)
        self.assertIn("second reading -- the listed limits are the whole rule", issues[0])

    def test_an_erring_judge_never_passes_and_is_asked_again(self):
        down = Judges(reading="YES", reading_quote=f'"{DECIDING}"', fail={"gpt-6-astra"})
        issues = attribution.reading_problems(self.bundle, self.report, ask=down)
        self.assertEqual(len(issues), 1, issues)
        self.assertIn("never passes", issues[0])
        self.assertIn("HTTP 529", issues[0])
        healthy = Judges(reading="YES", reading_quote=f'"{DECIDING}"')
        self.assertEqual(attribution.reading_problems(self.bundle, self.report, ask=healthy), [])
        self.assertEqual(json.loads((self.bundle / attribution.READINGS).read_text())
                         ["checks"][f"{STEP}/o_bogus_after"]["asked"], 2)

    def test_a_reworded_question_is_asked_again(self):
        attribution.reading_problems(self.bundle, self.report, ask=Judges(reading="UNDETERMINED"))
        settled = Judges(reading="YES", reading_quote=f'"{DECIDING}: BOGUS"')
        self.assertEqual(len(attribution.reading_problems(self.bundle, self.report, ask=settled)), 1)
        self.assertEqual(settled.asked("THE HIDDEN TEST SOURCE"), 0)       # the receipt answers the same question
        with patch.object(attribution, "READING_PROMPT", attribution.READING_PROMPT + "\n"):
            self.assertEqual(attribution.reading_problems(self.bundle, self.report, ask=settled), [])
        self.assertEqual(settled.asked("THE HIDDEN TEST SOURCE"), 2)

    def test_the_question_is_what_the_words_decide_not_how_hard_it_is(self):
        for words in ("not about how hard it is to get right", "a precision or parsing slip", "is not a reading",
                      "neither the instruction nor the repository ever shows is decided only when"):
            self.assertIn(words, attribution.READING_PROMPT)

    def test_a_refusing_judge_with_no_quote_never_passes(self):
        judges = Judges(reading="YES", reading_quote="NONE")
        issues = attribution.reading_problems(self.bundle, self.report, ask=judges)
        self.assertIn("without a deciding sentence found verbatim", issues[0])

    def test_judged_once_per_grading_sha(self):
        judges = Judges(reading="UNDETERMINED")
        attribution.reading_problems(self.bundle, self.report, ask=judges)
        n = len(judges.calls)
        fable_report = {**self.report, "model": FABLE}
        self.assertEqual(len(attribution.reading_problems(self.bundle, fable_report, ask=judges)), 1)
        self.assertEqual(len(judges.calls), n)                     # the other model's campaign reuses it
        entry = json.loads((self.bundle / attribution.READINGS).read_text())["checks"][f"{STEP}/o_bogus_after"]
        self.assertEqual(entry["fail_rate"], {ASTRA: 0.6, FABLE: 0.6})
        spec = json.loads((self.bundle / "authoring/spec.json").read_text())
        spec["steps"][0]["instruction"] += " each line's position and speeds follow from the line before."
        atomic_json(self.bundle / "authoring/spec.json", spec)
        settled = Judges(reading="YES", reading_quote=f'"{DECIDING}"')
        self.assertEqual(attribution.reading_problems(self.bundle, self.report, ask=settled), [])

    def test_release_reads_it_through_attribution_problems(self):
        with patch.object(attribution, "reading_problems", return_value=["X: reading_settled"]) as reading:
            issues = attribution.problems(self.bundle, self.report)
        reading.assert_called_once()
        self.assertIn("X: reading_settled", issues)
        with patch.object(attribution, "reading_problems", return_value=["X: reading_settled"]):
            summary = attribution.summary(self.bundle, [self.report])
        self.assertIn("X: reading_settled", summary["problems"])
        self.assertFalse(summary["attributable"])

    def test_quotes_are_found_through_typography(self):
        text = (FIXTURE / "instruction.md").read_text()
        self.assertTrue(attribution.quote_found("“the tank loses exactly the thrust that was used that turn”", text))
        self.assertTrue(attribution.quote_found('"the tilt is a whole number of degrees" ... "moves at most 15 in a '
                                                'turn"', text))
        self.assertFalse(attribution.quote_found("NONE", text))
        self.assertFalse(attribution.quote_found('"positions must follow the physics"', text))

    def test_non_adjacent_sentences_joined_without_an_ellipsis_are_found(self):
        text = ("کد را همان‌طور که مشتری تایپ کرده قبول کن. خط تیره جزو خود کد است. "
                "مبلغ تخفیف اگر اعشار داشت رو به پایین به تومان گرد می‌شود؛ تخفیف از مبلغ کالاها بیشتر نمی‌شود.")
        joined = "کد را همان‌طور که مشتری تایپ کرده قبول کن. مبلغ تخفیف اگر اعشار داشت رو به پایین به تومان گرد می‌شود؛"
        self.assertTrue(attribution.quote_found(joined, text))
        self.assertTrue(attribution.quote_found("Keep the name. Round every amount down to the cent.",
                                                "Keep the name. Totals are shown in euros. Round every amount down to the cent."))
        # every sentence must still be verbatim: one invented sentence fails the whole quote
        self.assertFalse(attribution.quote_found("Keep the name. Round every amount to the nearest cent.",
                                                 "Keep the name. Round every amount down to the cent."))
        self.assertFalse(attribution.quote_found("A single sentence that is not there.", "Keep the name."))
        text = "varchar(n) takes n characters at most. those are the types. the tables come out of the create script."
        self.assertTrue(attribution.quote_found("varchar(n) takes n characters at most. / the tables come out of the "
                                                "create script", text))
        self.assertTrue(attribution.quote_found("varchar(n) takes n characters at most. | the tables come out of the "
                                                "create script", text))
        self.assertFalse(attribution.quote_found("varchar(n) takes n characters at most. / the tables come from the "
                                                 "database", text))
        bengali = "লাইনের প্রথম শব্দটা হুবহু SAFE_PREFIXES এর একটা নাম। হুবহু মানে পুরো শব্দ। লাইনটা খালি হলে চলবে না।"
        self.assertTrue(attribution.quote_found("লাইনের প্রথম শব্দটা হুবহু SAFE_PREFIXES এর একটা নাম। লাইনটা খালি হলে চলবে না।",
                                                bengali))


if __name__ == "__main__":
    unittest.main()
