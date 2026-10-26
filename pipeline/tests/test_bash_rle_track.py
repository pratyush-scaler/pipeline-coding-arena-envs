"""The bash RLE track and the trace failure digest (user, 2026-10-26).

"we need to add a branch to the pipeline to create bash failure tasks which are rich in
bash failure outcomes, Lets add our best learnings and also while authoring and
designing the task let's ensure the designer has maximum amount of context on the
failures in the traces ... Not only for bash tasks instead for both coding and bash_rle
tasks."
"""
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

from pipeline import policy
from pipeline.author import agent_designer as a
from pipeline.author import bash_rle, levers
from pipeline.dump import failures
from pipeline.select import bash_rle as wave


def bash(command, exit_code=0, stderr="", stdout="", state="output-available"):
    return {"type": "tool-bash", "state": state, "input": {"command": command, "cwd": "/home/user/app"},
            "output": {"exit_code": exit_code, "stderr": stderr, "stdout": stdout, "duration_ms": 40}}


MESSAGES = [
    {"role": "user", "node_ids": ["u0"], "content": [{"type": "text", "text": "Package the delivery."}]},
    {"role": "assistant", "node_ids": ["a1"], "feedback": {"check_in": "edit"}, "content": [
        bash("pdfinfo gabarito.pdf | grep Pages", 1, "/bin/bash: line 1: pdfinfo: command not found\n"),
        bash("zip -r out.zip STL", 0),
        bash("./build.sh", 126, "/bin/bash: line 1: ./build.sh: Permission denied\n")]},
    {"role": "user", "node_ids": ["u2"], "content": [{"type": "text", "text": "Nunca apague o zip que já enviei."}]},
    {"role": "assistant", "node_ids": ["a3"], "feedback": {"check_in": "approve"}, "content": [
        bash('pkill -f "node server.js" || true', -1, ""),
        bash('pkill -f "node server.js" || true', -1, ""),
        bash("grep -n TODO src/*.js", 1, ""),
        {"type": "tool-fetch_page", "state": "output-available", "input": {"url": "https://example.com"},
         "output": {"error": "Failed to fetch page (HTTP 500)", "status": 500}}]},
    {"role": "user", "node_ids": ["u4"], "content": [{"type": "text", "text": "ok"}]},
]
OUTCOMES = {"outcomes": [
    # Message 1: three verdicts for three shell calls -> attached call by call, in index order.
    {"signal_name": "bash_reliability", "outcome": "failure", "observation_id": "a1::30"},
    {"signal_name": "bash_reliability", "outcome": "success", "observation_id": "a1::12"},
    {"signal_name": "bash_reliability", "outcome": "success", "observation_id": "a1::5"},
    # Message 3: two verdicts for three shell calls -> counted for the message, attached to no call.
    {"signal_name": "bash_reliability", "outcome": "failure", "observation_id": "a3::3"},
    {"signal_name": "bash_reliability", "outcome": "failure", "observation_id": "a3::9"},
    {"signal_name": "steerability", "outcome": "failure", "observation_id": "u2::1"},
]}


class FailureDigest(unittest.TestCase):
    def setUp(self):
        self.result = failures.digest({"messages": MESSAGES}, OUTCOMES, (0, 2))

    def test_classes_name_what_went_wrong(self):
        self.assertEqual(failures.classify("pdfinfo x", 127, "", ""), "missing_tool")
        self.assertEqual(failures.classify("./build.sh", 1, "./build.sh: Permission denied", ""), "permission")
        self.assertEqual(failures.classify("python3 x.py", 1, "ModuleNotFoundError: No module named 'pypdf'", ""),
                         "missing_module")
        self.assertEqual(failures.classify('pkill -f "node server.js"', -1, "", ""), "timeout_or_killed")
        self.assertEqual(failures.classify("cd /a && grep -n x f", 1, "", ""), "no_match")
        self.assertEqual(failures.classify("cat <<EOF", 2, "syntax error near unexpected token `)'", ""),
                         "quoting_syntax")
        self.assertEqual(failures.program("cd /x && sudo timeout 30 FOO=1 pdfinfo a.pdf"), "pdfinfo")

    def test_every_failed_call_is_listed_in_order_with_the_span_marked(self):
        rows = self.result["failures"]
        self.assertEqual([(r["msg"], r["class"]) for r in rows],
                         [(1, "missing_tool"), (1, "permission"), (3, "timeout_or_killed"),
                          (3, "timeout_or_killed"), (3, "no_match"), (3, "network")])
        self.assertEqual([r["in_span"] for r in rows], [True, True, False, False, False, False])
        self.assertEqual(rows[0]["program"], "pdfinfo")
        self.assertIn("pdfinfo: command not found", rows[0]["stderr"])
        self.assertEqual(rows[0]["agent_next"], "zip -r out.zip STL")
        s = self.result["summary"]
        self.assertEqual((s["shell_calls"], s["shell_failed"], s["shell_failed_in_span"], s["tool_errors"]),
                         (6, 5, 2, 1))
        self.assertEqual(s["missing_programs"], ["pdfinfo"])
        self.assertEqual(s["longest_failed_streak"], 4, "build.sh, both pkills and the grep, in a row")

    def test_arena_verdicts_attach_only_where_the_counts_match(self):
        rows = self.result["failures"]
        self.assertEqual(rows[0]["arena_bash_reliability"], "success")   # index 5: first call
        self.assertEqual(rows[1]["arena_bash_reliability"], "failure")   # index 30: third call
        self.assertIsNone(rows[2]["arena_bash_reliability"])             # 2 verdicts, 3 calls
        self.assertEqual(self.result["summary"]["arena_bash_reliability"], {"success": 2, "failure": 3})
        self.assertEqual(self.result["summary"]["arena_bash_reliability_in_span"], {"success": 2, "failure": 1})

    def test_repeats_and_the_users_reaction_are_recorded(self):
        rows = self.result["failures"]
        self.assertEqual([r["attempt"] for r in rows[2:4]], [1, 2])
        self.assertEqual(rows[0]["rejected_by_user"], "edit")
        self.assertIsNone(rows[2]["rejected_by_user"], "an approved response is not pushback")
        self.assertEqual(rows[0]["user_next"]["msg"], 2)
        self.assertEqual(rows[0]["user_next"]["signals"], ["steerability:failure"])
        self.assertIn("Nunca apague", rows[0]["user_next"]["text"])
        self.assertEqual(self.result["summary"]["followed_by_pushback"], 2)

    def test_the_brief_leads_with_what_a_designer_can_build_on(self):
        lines = failures.brief(self.result, rows=3)
        text = "\n".join(lines)
        self.assertIn("TRACE FAILURE DIGEST", lines[0])
        self.assertIn("['pdfinfo']", text)
        self.assertIn("read_failures(offset=3)", text)
        first = [line for line in lines if line.startswith("  - ")]
        self.assertIn("msg 1 [permission, exit 126, Arena failure]", first[0])
        self.assertNotIn("no_match", "\n".join(first), "a search that found nothing ranks last")
        quiet = failures.brief(failures.digest({"messages": MESSAGES[:1]}, None))
        self.assertEqual(len(quiet), 1)
        self.assertIn("none failed", quiet[0])


class DesignerGetsTheFailures(unittest.TestCase):
    def _bundle(self, tmp):
        (Path(tmp) / "environment/repo").mkdir(parents=True)
        return Path(tmp)

    def _extraction(self):
        return SimpleNamespace(session_id="sess0001", seed_index=0, pivot=1, span=(0, 2), moments=[],
                               changed_paths=[], stats={})

    def test_read_failures_is_offered_filtered_and_paged(self):
        tools = {t["name"]: t for t in a._tools(5)}
        self.assertIn("read_failures", tools)
        self.assertIn("6000", tools["read_trace"]["description"])
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch("pipeline.dump.signals.load_outcomes", return_value=OUTCOMES):
            extraction = self._extraction()
            extraction.source_root = Path(tmp)
            handlers = a._handlers(self._bundle(tmp), extraction, {"messages": MESSAGES})
            page = handlers["read_failures"](limit=2)
            self.assertEqual((page["total"], len(page["failures"]), page["next_offset"]), (6, 2, 2))
            only = handlers["read_failures"](failure_class="timeout_or_killed")
            self.assertEqual([r["attempt"] for r in only["failures"]], [1, 2])
            self.assertEqual(handlers["read_failures"](in_span_only=True)["total"], 2)

    def test_read_trace_shows_long_commands_and_stderr(self):
        long_err = "E" * 3000 + " the line that explains it"
        msg = {"role": "assistant", "content": [bash("make", 2, long_err)]}
        self.assertNotIn("the line that explains it", a._render_message(msg, limit=None))
        self.assertIn("the line that explains it",
                      a._render_message(msg, limit=None, tool_chars=a.TRACE_TOOL_IO_CHARS))

    def test_every_opening_carries_the_digest_and_only_the_track_carries_the_mandate(self):
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch("pipeline.dump.signals.load_outcomes", return_value=OUTCOMES):
            opening = a._opening({"tier": 2, "classification": {}}, self._extraction(), self._bundle(tmp), "F2",
                                 {"messages": MESSAGES})
        self.assertIn("TRACE FAILURE DIGEST", opening)
        self.assertIn("ARENA SIGNALS for this session", opening)
        self.assertIn("bash_reliability failure 3, success 2 (failures at msgs [1, 3])", opening)
        self.assertIn("steerability failure 1 (failures at msgs [2])", opening)
        self.assertIn("USER CHECK-INS on the agent's responses", opening)
        self.assertIn("approve at msgs [3]; edit at msgs [1]", opening)
        self.assertIn("read_failures", opening)
        self.assertEqual(bash_rle.lead({"track": "coding"}, opening), opening)
        led = bash_rle.lead({"track": bash_rle.TRACK}, opening)
        self.assertTrue(led.startswith("TRACK: BASH RLE"))
        self.assertTrue(led.endswith(opening))

    def test_the_mandate_names_catalogued_measured_levers(self):
        for name in bash_rle.LEVERS:
            self.assertIn(name, levers.LEVERS)
            self.assertIn(name, bash_rle.MANDATE)
        measured = {levers.LEVERS[n]["targets"] for n in bash_rle.LEVERS if levers.LEVERS[n]["status"] == "measured"}
        self.assertTrue({"fable", "astra", "both"} <= measured)
        self.assertIn("/proc/self", bash_rle.MANDATE)
        self.assertTrue(any("pkill -f" in d for d in levers.DEAD))
        self.assertIn("pkill -f", levers.PROMPT)


class BashRleWave(unittest.TestCase):
    ROWS = [
        {"seed_key": "rich::0", "session_id": "rich", "span": [0, 4], "tier": 2,
         "classification": {"survives": True}},
        {"seed_key": "thin::0", "session_id": "thin", "span": [0, 0], "tier": 1,
         "classification": {"survives": True}},
        {"seed_key": "dead::0", "session_id": "rich", "span": [0, 4], "tier": 1,
         "classification": {"survives": False}},
        {"seed_key": "tier1::0", "session_id": "rich", "span": [0, 2], "tier": 1,
         "classification": {"survives": True}},
    ]

    def test_seeds_rich_in_failed_shell_decisions_are_stamped_and_ordered(self):
        with unittest.mock.patch("pipeline.dump.recover.load_trace", return_value={"messages": MESSAGES}), \
                unittest.mock.patch("pipeline.dump.signals.load_outcomes", return_value=OUTCOMES):
            picked, skipped = wave.compose(self.ROWS)
        self.assertEqual([r["seed_key"] for r in picked], ["tier1::0", "rich::0"])
        self.assertTrue(all(r["track"] == bash_rle.TRACK for r in picked))
        rich = picked[1]["bash_profile"]
        self.assertEqual(rich["failed_shell_calls"], 5)
        self.assertEqual(rich["decision_failures"], 4, "the empty grep is not a shell decision")
        self.assertEqual(rich["classes"], {"timeout_or_killed": 2, "missing_tool": 1, "permission": 1})
        self.assertGreater(picked[1]["track_priority"], picked[0]["track_priority"])
        self.assertEqual([s["seed_key"] for s in skipped], ["thin::0"])
        self.assertIn(f"fewer than {policy.BASH_RLE_MIN_FAILED_SHELL_CALLS}", skipped[0]["reason"])

    def test_the_wave_file_is_a_classified_snapshot_build_can_read(self):
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch("pipeline.dump.recover.load_trace", return_value={"messages": MESSAGES}), \
                unittest.mock.patch("pipeline.dump.signals.load_outcomes", return_value=OUTCOMES), \
                unittest.mock.patch("pipeline.select.bash_rle.sessions_root", return_value=Path(tmp)):
            src, out = Path(tmp) / "classified.json", Path(tmp) / "wave.json"
            src.write_text(json.dumps({"schema": "arena-pipeline/classified/4", "rows": self.ROWS}))
            self.assertEqual(wave.main(["-c", str(src), "-o", str(out), "-n", "1"]), 0)
            doc = json.loads(out.read_text())
        self.assertEqual(doc["schema"], "arena-pipeline/classified/4")
        self.assertEqual([r["seed_key"] for r in doc["rows"]], ["tier1::0"])
        self.assertEqual(doc["track"], bash_rle.TRACK)


if __name__ == "__main__":
    unittest.main()
