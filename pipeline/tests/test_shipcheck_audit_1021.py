"""Ship-time checks added after the external audit of 2026-10-21 (brief classes 4 and 6).

Every fixture is lifted from a shipped Batch 3 package (pipeline/tests/fixtures/audit_1021):

  pptx-generator-script   g_no_shadow lists 'conftest' (a correct solution adding tests/conftest.py
                          scores 0); "вид слайдов не меняй — шрифты, цвета и размеры как есть" is
                          enforced by a guardrail that checks one colour
  static-html-mindmap     "页面的结构、id 和 class ... 还有样式都别动" is quoted only by a shadow-guard
                          over playwright and two objectives that never compare styles
  slide-pptx-generation   g_deck_shape zeroed Fable run package__4DUqEWP (earned 20) on a
                          core-properties assertion its sentence never mentions

Both calibration families are scripted (`Families`); no test reaches a network. A family that errs or
answers nothing must block, never pass.
"""
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import clause_map, hack_triage, shipcheck

FIXTURES = Path(__file__).resolve().parent / "fixtures/audit_1021"
FABLE, ASTRA = "claude-fable-5-1", "gpt-6-astra"

# Verbatim clauses of the shipped instructions.
RU_NAMES = "имена файлов и команды не меняй"
RU_LOOK = "вид слайдов не меняй — шрифты, цвета и размеры как есть."
RU_NUMBERS = "номера комнат проставляй сам, подряд: 01, 02, 03 и дальше."
RU_CONTENT = "tour_content.py не трогай"
ZH_STYLES = "页面的结构、id 和 class（.kid / .n / .lbl / .bdg 这些）还有样式都别动，我要的就是现在这个长相。"
ZH_WORDING = "措辞照旧"
ZH_PUNCT = "括号这类符号我也会往搜索框里打，不管打进去什么，树该怎么筛就怎么筛，别给我卡死在那儿。"
LOOK_COUNTER = ("build_tour.py paints the body text green and the headings white: test_deck_keeps_its_look reads "
                "only sizes, faces and that FBBF24 appears, so no test fails")


class Families:
    """Both calibration families, scripted. `extract[model]` are the quotes it returns, `judge(model, quote)` its
    (YES|NO, counter-example) per clause, `guard(model)` its guardrail verdict; a model in `fail` raises on every
    call, one in `refuse` refuses (hack_triage.JudgeRefused), one in `prose` answers without the JSON block."""

    def __init__(self, extract=None, judge=None, guard=None, fail=(), refuse=(), prose=()):
        self.extract = extract or {}
        self.judge = judge or (lambda model, quote: ("YES", ""))
        self.guard = guard or (lambda model: "INSIDE")
        self.fail, self.refuse, self.prose = set(fail), set(refuse), set(prose)
        self.calls = []

    def __call__(self, model, prompt, effort="medium", max_tokens=0):
        kind = ("extract" if prompt.startswith("A user wrote the instruction below") else
                "judge" if "Hidden tests grade the developer's work" in prompt else
                "guard" if "One GUARDRAIL check failed" in prompt else "?")
        self.calls.append((model, kind))
        if model in self.fail:
            raise RuntimeError("HTTP 529: overloaded")
        if model in self.refuse:
            raise hack_triage.JudgeRefused(f"{model} refused (stop_reason refusal)")
        if model in self.prose:
            return "I think the checks look fine overall."
        if kind == "extract":
            rows = [{"quote": q, "promise": "-"} for q in self.extract.get(model, self.extract.get("*", []))]
            return "```json\n" + json.dumps({"clauses": rows}, ensure_ascii=False) + "\n```"
        if kind == "judge":
            listed = prompt.split("CLAUSES (id: the clause -- the checks that quote it)\n-------\n", 1)[1]
            rows = []
            for line in listed.splitlines():
                m = re.match(r'(c\d+): (".*?") -- ', line)
                if m:
                    verdict, example = self.judge(model, json.loads(m.group(2)))
                    rows.append({"id": m.group(1), "detects": verdict, "counter_example": example})
            return "Reasoning.\n```json\n" + json.dumps({"clauses": rows}, ensure_ascii=False) + "\n```"
        if kind == "guard":
            return f"The failing assertion reads the document properties.\nVERDICT: {self.guard(model)}"
        raise AssertionError(f"unexpected prompt: {prompt[:120]}")

    def asked(self, kind):
        return [m for m, k in self.calls if k == kind]


class AuditBundle(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def bundle(self, name, step, *, suites=()):
        src = FIXTURES / name
        b = self.root / name
        (b / "steps" / step).mkdir(parents=True)
        (b / "tests/suites").mkdir(parents=True)
        (b / "verification").mkdir()
        shutil.copy(src / "instruction.md", b / "steps" / step / "instruction.md")
        for f in ("checks.json", "basis.toml", "fixture-manifest.json"):
            if (src / f).is_file():
                shutil.copy(src / f, b / "tests" / f)
        for fixture, rel in suites:
            shutil.copy(src / fixture, b / "tests/suites" / rel)
        return b

    def pptx(self):
        return self.bundle("pptx-generator-script", "01-bold-script", suites=[("test_look.py.fixture", "test_look.py")])

    def mindmap(self):
        b = self.bundle("static-html-mindmap", "01-mindmap-behaviour")
        shutil.copy(FIXTURES / "static-html-mindmap/probes.json", b / "verification/probes.json")
        return b

    def edit_checks(self, b, fn):
        doc = json.loads((b / "tests/checks.json").read_text())
        doc["checks"] = fn(doc["checks"])
        (b / "tests/checks.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------------------
# 1. guardrail.shadow_conftest
# ---------------------------------------------------------------------------

class ShadowConftest(AuditBundle):
    def test_conftest_among_shadow_modules_is_a_finding_and_the_grader_proves_it(self):
        from pipeline.author.grader_runtime import grade as runtime
        b = self.pptx()
        found = shipcheck.guardrail_shadow_conftest(b)
        self.assertEqual([(f["check"], f["subject"], f["route"], f["measure"]) for f in found],
                         [("guardrail.shadow_conftest", "tests/checks.json#g_no_shadow", "harden", "regrade")])
        self.assertIn("added-conftest exemption", found[0]["detail"])
        fix = json.loads(found[0]["evidence"][0])["check_set"][0]
        self.assertEqual(fix["value"]["modules"], ["pptx", "markdown_it", "tourcheck", "handwork"])

        # Executed with the shipped grader runtime: a correct solution plus its own local test helper.
        check = next(c for c in json.loads((b / "tests/checks.json").read_text())["checks"] if c["id"] == "g_no_shadow")
        manifest = json.loads((b / "tests/fixture-manifest.json").read_text())
        repo = self.root / "repo"
        for rel in manifest:
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text("# fixture\n")
        (repo / "tests").mkdir()
        (repo / "tests/conftest.py").write_text("import pytest\n")
        (repo / "tests/test_local.py").write_text("def test_ok():\n    assert True\n")
        ok, why = runtime.structural(check, repo, manifest)
        self.assertFalse(ok)
        self.assertIn("tests/conftest.py", why)
        self.assertEqual(runtime.structural({**check, "runner": fix["value"]}, repo, manifest),
                         (True, "no shadowing entry"))

    def test_any_entry_whose_last_component_is_conftest_counts(self):
        b = self.pptx()
        for entry in ("tests/conftest", "conftest.py", "pkg/tests/conftest.py"):
            self.edit_checks(b, lambda cs: [{**c, "runner": {"type": "shadow-guard", "modules": ["pptx", entry]}}
                                            if c["id"] == "g_no_shadow" else c for c in cs])
            with self.subTest(entry=entry):
                self.assertEqual(len(shipcheck.guardrail_shadow_conftest(b)), 1)
        self.edit_checks(b, lambda cs: [{**c, "runner": {"type": "shadow-guard", "modules": ["pptx", "conftests"]}}
                                        if c["id"] == "g_no_shadow" else c for c in cs])
        self.assertEqual(shipcheck.guardrail_shadow_conftest(b), [])

    def test_a_shadow_guard_without_conftest_is_clean(self):
        self.assertEqual(shipcheck.guardrail_shadow_conftest(self.mindmap()), [])     # modules ["playwright"]


# ---------------------------------------------------------------------------
# 2. clause map: (a) extract, (b) quoted, (c) whole
# ---------------------------------------------------------------------------

RU_FAMILIES = {FABLE: [RU_NAMES, RU_LOOK, RU_NUMBERS, RU_CONTENT],
               # Astra quotes the look clause with other spacing and no full stop, the content clause as the
               # whole sentence, and one promise in English, which is not in the instruction and is rejected.
               ASTRA: ["вид слайдов не меняй —  шрифты, цвета и размеры как есть",
                       "tour_content.py не трогай, я его сам правлю и дописываю туда новые слайды и новые слова.",
                       "keep fonts, colours and sizes as they are"]}


class ClauseMap(AuditBundle):
    def ensure(self, b, families):
        return clause_map.ensure(b, ask=families)

    def statuses(self, record):
        return {c["quote"]: c["status"] for row in record["steps"].values() for c in row["clauses"]}

    def test_a_russian_keep_the_look_clause_with_no_enforcing_check_is_unenforced(self):
        b = self.pptx()
        self.edit_checks(b, lambda cs: [c for c in cs if c["id"] != "g_deck_look"])
        families = Families(extract=RU_FAMILIES)
        record = self.ensure(b, families)
        found = clause_map.findings(record)
        self.assertEqual([(f["check"], f["route"], f["measure"]) for f in found],
                         [("coverage.clause_unenforced", "harden", "regrade")])
        self.assertIn("вид слайдов не меняй", found[0]["subject"])
        step = record["steps"]["01-bold-script"]
        self.assertEqual(step["extraction"][ASTRA]["rejected"], ["keep fonts, colours and sizes as they are"])
        look = next(c for c in step["clauses"] if c["quote"] == RU_LOOK)
        self.assertEqual((look["by"], look["quoted_by"]), ([FABLE, ASTRA], []))
        self.assertEqual(self.statuses(record)[RU_NUMBERS], "enforced")

    def test_a_check_that_quotes_the_clause_but_tests_one_colour_is_in_name_only(self):
        b = self.pptx()
        families = Families(extract=RU_FAMILIES,
                            judge=lambda model, quote: ("NO", LOOK_COUNTER) if model == ASTRA and "шрифты" in quote
                            else ("YES", ""))
        record = self.ensure(b, families)
        found = clause_map.findings(record)
        self.assertEqual([f["check"] for f in found], ["coverage.clause_in_name_only"])
        self.assertIn("g_deck_look", found[0]["detail"])
        self.assertIn(LOOK_COUNTER, found[0]["detail"])
        self.assertEqual(found[0]["route"], "harden")
        # The judges read the guardrail's own test: the suite it runs comes first in their prompt.
        prompt_suites = clause_map._suites_text(b, {"test_look.py"})
        self.assertTrue(prompt_suites.startswith("=== test_look.py\n"))

    def test_a_chinese_keep_the_styles_clause_quoted_by_a_shadow_guard_is_in_name_only(self):
        b = self.mindmap()
        families = Families(extract={"*": [ZH_STYLES, ZH_WORDING, ZH_PUNCT]},
                            judge=lambda model, quote: ("NO", "rename li.kid to li.kd in the CSS and the script, make "
                                                              ".n.gray borders solid: no case reads either")
                            if quote == ZH_STYLES else ("YES", ""))
        found = clause_map.findings(self.ensure(b, families))
        self.assertEqual([f["check"] for f in found], ["coverage.clause_in_name_only"])
        self.assertIn("g_no_shadow", found[0]["detail"])
        self.assertIn("li.kd", found[0]["detail"])

    def test_a_chinese_clause_no_check_quotes_needs_a_determinacy_reason_to_be_untested(self):
        import tomllib
        b = self.mindmap()
        basis = tomllib.loads((b / "tests/basis.toml").read_text())
        (b / "authoring").mkdir()
        (b / "authoring/spec.json").write_text(json.dumps({"checks": [
            {"id": cid, "rule_quote": [q for q in row.get("rule_quote") or [] if q != ZH_STYLES]}
            for cid, row in basis.items()]}, ensure_ascii=False))
        families = Families(extract={"*": [ZH_STYLES, ZH_PUNCT]})
        found = clause_map.findings(self.ensure(b, families))
        self.assertEqual([f["check"] for f in found], ["coverage.clause_unenforced"])
        probes = json.loads((b / "verification/probes.json").read_text())
        probes["untested_surfaces"].append(f"「{ZH_STYLES}」 -- no hidden case compares the stylesheet")
        (b / "verification/probes.json").write_text(json.dumps(probes, ensure_ascii=False))
        self.assertEqual([f["check"] for f in clause_map.findings(self.ensure(b, families))],
                         ["coverage.clause_unenforced"])                  # feasibility is not a reason
        probes["untested_surfaces"].append(f"「{ZH_STYLES}」 -- the determinacy probe answered undetermined: both "
                                           "readings of 样式 are defensible")
        (b / "verification/probes.json").write_text(json.dumps(probes, ensure_ascii=False))
        record = self.ensure(b, families)
        self.assertEqual(clause_map.findings(record), [])
        self.assertEqual(self.statuses(record)[ZH_STYLES], "untested")

    def test_an_english_leave_it_sentence_is_seeded_even_when_both_families_miss_it(self):
        b = self.root / "fastapi"
        (b / "steps/01-x").mkdir(parents=True)
        (b / "tests").mkdir()
        (b / "steps/01-x/instruction.md").write_text("make the api return the saved notes. leave the html for now "
                                                     "in index.html.\n")
        (b / "tests/fixture-manifest.json").write_text(json.dumps({"files": {"app/frontend/index.html": {}}}))
        (b / "tests/checks.json").write_text(json.dumps({"checks": [
            {"id": "o_notes", "kind": "objective", "step": "01-x", "runner": {"type": "pytest", "select": ["t.py"]},
             "rule_quote": ["make the api return the saved notes."]}]}))
        found = clause_map.findings(self.ensure(b, Families(extract={"*": ["make the api return the saved notes."]})))
        self.assertEqual([f["check"] for f in found], ["coverage.clause_unenforced"])
        self.assertIn("leave the html for now", found[0]["detail"])
        self.assertEqual(found[0]["evidence"], ["seed:preservation"])


class ErringJudges(AuditBundle):
    """An erring judge never passes anything: its clause stays unadjudicated and blocks."""

    def test_a_family_that_errs_blocks_extraction_and_every_judgement_it_owes(self):
        b = self.pptx()
        families = Families(extract=RU_FAMILIES, fail=[ASTRA])
        record = clause_map.ensure(b, ask=families)
        found = clause_map.findings(record)
        self.assertFalse(record["complete"])
        self.assertTrue(found)
        self.assertEqual({f["check"] for f in found}, {"coverage.clause_map"})
        self.assertNotIn("enforced", {c["status"] for c in record["steps"]["01-bold-script"]["clauses"]})
        self.assertEqual(families.asked("extract").count(ASTRA), 2)               # medium, then low effort

    def test_a_reply_without_verdicts_is_no_verdict(self):
        b = self.pptx()
        record = clause_map.ensure(b, ask=Families(extract=RU_FAMILIES, prose=[FABLE]))
        self.assertIn("unparsable", record["steps"]["01-bold-script"]["extraction"][FABLE]["error"])
        self.assertTrue(all(c["status"] in ("unadjudicated", "unenforced")
                            for c in record["steps"]["01-bold-script"]["clauses"]))

    def test_a_refusing_family_answers_through_its_next_model(self):
        b = self.pptx()
        families = Families(extract={**RU_FAMILIES, "claude-opus-5-5": RU_FAMILIES[FABLE]}, refuse=[FABLE])
        record = clause_map.ensure(b, ask=families)
        step = record["steps"]["01-bold-script"]
        self.assertEqual(step["extraction"][FABLE]["judge"], "claude-opus-5-5")
        self.assertIn(RU_NUMBERS, [c["quote"] for c in step["clauses"]])
        self.assertEqual({c["judgement"][FABLE].get("judge") for c in step["clauses"]}, {"claude-opus-5-5"})
        self.assertEqual(clause_map.findings(record), [])

    def test_a_judgement_missing_from_the_reply_blocks_that_clause_only(self):
        b = self.pptx()
        families = Families(extract=RU_FAMILIES)
        original = families.__call__

        def drop_numbers(model, prompt, **kw):
            reply = original(model, prompt, **kw)
            if "Hidden tests grade" in prompt and model == ASTRA:
                doc = json.loads(reply.split("```json\n", 1)[1].split("\n```", 1)[0])
                doc["clauses"] = [r for r in doc["clauses"] if not re.search(
                    rf'{r["id"]}: "номера', prompt)]
                return "```json\n" + json.dumps(doc, ensure_ascii=False) + "\n```"
            return reply
        record = clause_map.ensure(b, ask=drop_numbers)
        found = clause_map.findings(record)
        self.assertEqual([f["check"] for f in found], ["coverage.clause_map"])
        self.assertIn("номера комнат", found[0]["subject"])

    def test_errors_are_asked_again_and_only_what_is_missing(self):
        b = self.pptx()
        # Astra, once it answers, quotes nothing Fable did not: Fable owes no new judgement.
        quotes = {FABLE: RU_FAMILIES[FABLE], ASTRA: RU_FAMILIES[ASTRA][:1]}
        clause_map.ensure(b, ask=Families(extract=quotes, fail=[ASTRA]))
        again = Families(extract=quotes)
        record = clause_map.ensure(b, ask=again)
        self.assertTrue(record["complete"])
        self.assertEqual(clause_map.findings(record), [])
        self.assertEqual(set(again.asked("extract")) | set(again.asked("judge")), {ASTRA})


class Receipt(AuditBundle):
    def test_the_map_is_bound_to_the_grading_sha_and_reuses_an_unchanged_instruction(self):
        b = self.pptx()
        first = Families(extract=RU_FAMILIES)
        record = clause_map.ensure(b, ask=first)
        stored = json.loads((b / clause_map.RECEIPT).read_text())
        self.assertEqual(stored["grading_sha256"], clause_map.grading_sha(b))
        self.assertEqual(stored["grading_sha256"], record["grading_sha256"])
        quiet = Families(extract=RU_FAMILIES)
        clause_map.ensure(b, ask=quiet)
        self.assertEqual(quiet.calls, [])                                      # current: nothing asked
        (b / "tests/suites/test_look.py").write_text((b / "tests/suites/test_look.py").read_text() + "\n# more\n")
        suites_changed = Families(extract=RU_FAMILIES)
        clause_map.ensure(b, ask=suites_changed)
        self.assertEqual(suites_changed.asked("extract"), [])                  # the instruction did not change
        self.assertEqual(sorted(suites_changed.asked("judge")), [FABLE, ASTRA])
        inst = b / "steps/01-bold-script/instruction.md"
        inst.write_text(inst.read_text() + "\nи пусть .md всегда кончается переводом строки.\n")
        reworded = Families(extract=RU_FAMILIES)
        clause_map.ensure(b, ask=reworded)
        self.assertEqual(sorted(reworded.asked("extract")), [FABLE, ASTRA])


# ---------------------------------------------------------------------------
# 3. runs.guardrail_zeroed
# ---------------------------------------------------------------------------

STEP = "01-pptx-that-opens"


class GuardrailZeroed(AuditBundle):
    def slide_bundle(self, trials=("package__4DUqEWP",), grade=None):
        b = self.bundle("slide-pptx-generation", STEP)
        record = grade or json.loads((FIXTURES / "slide-pptx-generation/arena-grade-package__4DUqEWP.json").read_text())
        campaign = "runs/claude-fable-5-1/calibration-35b435f2216c4a69b5633578c4916ae0"
        rows = []
        for n, tid in enumerate(trials):
            folder = b / campaign / "jobs/run" / tid / "steps" / STEP / "verifier"
            folder.mkdir(parents=True)
            text = json.dumps(record).replace("work-coze_5v3", f"work-{n}abc").replace("0x783af3273d40", f"0x{n}ff")
            (folder / "arena-grade.json").write_text(text)
            rows.append({"trial_id": tid, "reward": record["reward"], "exception": False,
                         "per_step_rubric": {STEP: {"score": record["reward"]}}})
        (b / "runs").mkdir(exist_ok=True)
        (b / "runs/index.json").write_text(json.dumps({"campaigns": [
            {"label": "claude-fable-5-1", "path": campaign, "trials": rows}]}))
        return b

    def zeroed(self, b, families):
        return shipcheck.runs_guardrail_zeroed(b, shipcheck.shipped_trials(b / "runs"), ask=families)

    def test_a_guardrail_that_zeroed_earned_points_outside_its_sentence_blocks(self):
        b = self.slide_bundle()
        families = Families(guard=lambda model: "OUTSIDE" if model == FABLE else "INSIDE")
        found = self.zeroed(b, families)
        self.assertEqual([(f["check"], f["route"], f["measure"]) for f in found],
                         [("runs.guardrail_zeroed", "harden", "regrade")])
        detail = found[0]["detail"]
        self.assertIn("g_deck_shape", detail)
        self.assertIn("claude-fable-5-1/package__4DUqEWP earned 20", detail)
        self.assertIn("the package declares 0 deck properties relationships", detail)
        self.assertIn("same 16:9, same white master and theme", detail)
        receipt = json.loads((b / shipcheck.GUARDRAIL_RECEIPT).read_text())
        self.assertEqual(receipt["grading_sha256"], clause_map.grading_sha(b))
        self.assertEqual([r["verdict"] for r in receipt["rows"]], ["outside"])

    def test_both_families_inside_is_adjudicated_and_not_asked_again(self):
        b = self.slide_bundle()
        self.assertEqual(self.zeroed(b, Families()), [])
        again = Families(guard=lambda model: "OUTSIDE")
        self.assertEqual(self.zeroed(b, again), [])
        self.assertEqual(again.calls, [])
        (b / "tests/suites/test_deck.py").write_text("# Deck.core_part made lazy\n")      # a hidden-only repair
        rejudged = Families()
        self.zeroed(b, rejudged)
        self.assertEqual(sorted(rejudged.asked("guard")), [FABLE, ASTRA])

    def test_an_erring_or_unclear_judge_blocks(self):
        b = self.slide_bundle()
        found = self.zeroed(b, Families(fail=[ASTRA]))
        self.assertEqual([(f["check"], f["route"]) for f in found], [("runs.guardrail_zeroed", "regenerate")])
        self.assertIn("not adjudicated", found[0]["detail"])
        retry = Families()
        self.assertEqual(self.zeroed(b, retry), [])
        self.assertEqual(retry.asked("guard"), [ASTRA])                     # Fable's INSIDE was kept
        unclear = self.root / "u"
        shutil.copytree(b, unclear)
        (unclear / shipcheck.GUARDRAIL_RECEIPT).unlink()
        found = self.zeroed(unclear, Families(guard=lambda model: "UNCLEAR" if model == ASTRA else "INSIDE"))
        self.assertEqual([(f["check"], f["route"]) for f in found], [("runs.guardrail_zeroed", "harden")])

    def test_the_same_assertion_in_several_runs_is_judged_once(self):
        b = self.slide_bundle(trials=("package__4DUqEWP", "package__Other"))
        families = Families(guard=lambda model: "OUTSIDE")
        found = self.zeroed(b, families)
        self.assertEqual(len(found), 1)
        self.assertIn("zeroed 2 shipped episode(s)", found[0]["detail"])
        self.assertEqual(sorted(families.asked("guard")), [FABLE, ASTRA])

    def test_a_guardrail_failure_with_nothing_earned_needs_no_adjudication(self):
        grade = json.loads((FIXTURES / "slide-pptx-generation/arena-grade-package__4DUqEWP.json").read_text())
        grade["earned"] = 0
        for rec in grade["checks"].values():
            if rec["kind"] == "objective":
                rec["passed"] = False
        families = Families(guard=lambda model: "OUTSIDE")
        self.assertEqual(self.zeroed(self.slide_bundle(grade=grade), families), [])
        self.assertEqual(families.calls, [])


# ---------------------------------------------------------------------------
# 4. the sweep and the build-time readiness
# ---------------------------------------------------------------------------

class Wiring(AuditBundle):
    def complete(self, b, name):
        """What the rest of the sweep needs to be clean: task.toml, the current grader, a critic receipt."""
        from pipeline import coverage_critic
        from pipeline.artifacts import identity
        from pipeline.author import runtime
        step = next(p.name for p in (b / "steps").iterdir())
        (b / "task.toml").write_text(f'[task]\nname = "arena/{name}"\n\n[[steps]]\nname = "{step}"\n'
                                     '[steps.verifier]\ntimeout_sec = 100000.0\n')
        runtime.stage(b)
        (b / coverage_critic.RECEIPT).write_text(json.dumps({
            "artifact_sha256": identity(b), "steps": {step: {"proposals": [{"applied": True, "reward": 0.4}]}},
            "gaps": []}))

    def test_the_sweep_blocks_on_each_new_class(self):
        b = self.pptx()
        self.complete(b, "a_f1_pptx-generator-script")
        families = Families(extract=RU_FAMILIES,
                            judge=lambda model, quote: ("NO", LOOK_COUNTER) if "шрифты" in quote else ("YES", ""))
        with mock.patch.object(clause_map, "_ask", families):
            report = shipcheck.sweep(b)
        self.assertFalse(report["ok"])
        self.assertEqual(sorted(f["check"] for f in report["findings"]),
                         ["coverage.clause_in_name_only", "guardrail.shadow_conftest"])
        self.assertEqual(report["measure"], "regrade")

    def test_a_clean_bundle_with_clean_runs_has_no_finding(self):
        b = self.pptx()
        self.complete(b, "a_f1_pptx-generator-script")
        self.edit_checks(b, lambda cs: [{**c, "runner": {**c["runner"], "modules": ["pptx", "markdown_it", "tourcheck",
                                                                                    "handwork"]}}
                                        if c["id"] == "g_no_shadow" else c for c in cs])
        # A shipped run whose guardrails all held, certified.
        grade = json.loads((FIXTURES / "slide-pptx-generation/arena-grade-package__4DUqEWP.json").read_text())
        grade["checks"] = {"g_deck_look": {"kind": "guardrail", "weight": 0, "passed": True, "reason": "exit 0"},
                           "o_numbering": {"kind": "objective", "weight": 1, "passed": True, "reason": "exit 0"}}
        grade.update(step="01-bold-script", earned=1, reward=0.01, guardrail_violated=False)
        campaign = "runs/claude-fable-5-1/calibration-1"
        folder = b / campaign / "jobs/run/package__clean/steps/01-bold-script/verifier"
        folder.mkdir(parents=True)
        (folder / "arena-grade.json").write_text(json.dumps(grade))
        (b / "runs/index.json").write_text(json.dumps({"campaigns": [{"label": FABLE, "path": campaign, "trials": [
            {"trial_id": "package__clean", "exception": False, "per_step_rubric": {"01-bold-script": {}}}]}]}))
        (b / "verification/genuine.json").write_text(json.dumps({"certified": True, "episodes": [{"id": "package__clean"}]}))
        self.complete(b, "a_f1_pptx-generator-script")                      # identity after the checks edit
        families = Families(extract=RU_FAMILIES)
        with mock.patch.object(clause_map, "_ask", families):
            report = shipcheck.sweep(b)
        self.assertEqual(report["findings"], [])
        self.assertTrue(report["ok"])
        self.assertEqual(report["episodes"], 1)
        self.assertEqual(families.asked("guard"), [])
        self.assertTrue((b / clause_map.RECEIPT).is_file())

    def test_preservation_regex_left_the_blocking_sweep_for_the_map(self):
        b = self.root / "fastapi"
        (b / "steps/01-x").mkdir(parents=True)
        (b / "tests/suites").mkdir(parents=True)
        (b / "verification").mkdir()
        (b / "steps/01-x/instruction.md").write_text("leave the html for now in index.html.\n")
        (b / "tests/fixture-manifest.json").write_text(json.dumps({"files": {"app/frontend/index.html": {}}}))
        (b / "tests/checks.json").write_text(json.dumps({"checks": []}))
        self.complete(b, "a_f2_fastapi")
        with mock.patch.object(clause_map, "_ask", Families()):
            checks = [f["check"] for f in shipcheck.sweep(b)["findings"]]
        self.assertNotIn("coverage.preservation", checks)
        self.assertIn("coverage.clause_unenforced", checks)
        self.assertEqual([f["check"] for f in shipcheck.preservation_unguarded(b)], ["coverage.preservation"])

    def test_build_time_readiness_returns_them_as_repair_feedback(self):
        from pipeline import coverage_critic, orchestrate
        b = self.pptx()
        (b / coverage_critic.RECEIPT).write_text("{}")
        families = Families(extract=RU_FAMILIES,
                            judge=lambda model, quote: ("NO", LOOK_COUNTER) if "шрифты" in quote and model == ASTRA
                            else ("YES", ""))
        with mock.patch("pipeline.author.sandbox.available", return_value=(False, "no docker")), \
             mock.patch("pipeline.credential.resolve_credential", return_value="token"), \
             mock.patch.object(clause_map, "_ask", families):
            blockers = orchestrate._ship_readiness(b)
        by = {x["code"]: x for x in blockers}
        self.assertEqual(sorted(by), ["coverage.clause_in_name_only", "guardrail.shadow_conftest"])
        self.assertEqual(by["coverage.clause_in_name_only"]["where"], "tests/suites")
        self.assertEqual(by["guardrail.shadow_conftest"]["where"], "authoring/spec.json")
        self.assertIn("m_partial_", by["coverage.clause_in_name_only"]["message"])

    def test_build_time_leaves_an_unanswered_judge_to_the_ship_sweep(self):
        from pipeline import coverage_critic, orchestrate
        b = self.pptx()
        (b / coverage_critic.RECEIPT).write_text("{}")
        with mock.patch("pipeline.author.sandbox.available", return_value=(False, "no docker")), \
             mock.patch("pipeline.credential.resolve_credential", return_value="token"), \
             mock.patch.object(clause_map, "_ask", Families(extract=RU_FAMILIES, fail=[ASTRA])):
            codes = [x["code"] for x in orchestrate._ship_readiness(b)]
        self.assertEqual(codes, ["guardrail.shadow_conftest"])
        record = json.loads((b / clause_map.RECEIPT).read_text())
        self.assertFalse(record["complete"])                                # the ship-time sweep still blocks


if __name__ == "__main__":
    unittest.main()
