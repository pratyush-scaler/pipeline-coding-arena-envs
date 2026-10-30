"""The bash RLE track: coding tasks rich in bash failure outcomes.

User, 2026-10-26: "we need to add a branch to the pipeline to create bash failure
tasks which are rich in bash failure outcomes, Lets add our best learnings".

Arena asked (2026-10-22) for coding environments rich in failures of their
`bash_reliability` signal: shell calls that fail for reasons other than a missing
prerequisite. The first wave was built by hand, each row carrying an operator
brief. This track carries what that wave measured instead, so every bash RLE design
starts from it:

  shipped     sports-data-pipeline 0/10 Fable, 0/10 Astra; stl-release-packager
              0/10, 0/10; textforge-build 1/10, 0/10 (Harbor 0.24.0, 10 runs per
              model). 5-19% of each model's shell calls failed.
  easy        c-001 node-server-lifecycle, built on one famous pitfall (pkill -f
              matching its own shell): Fable 1.0 on 9 of 9 valid runs, Astra 0.9 on
              5 of 5; 6 runs voided by the unpublished /proc/self scanner rule.
  churn       the python site generator: 10 review rounds, each a new disagreement
              between the grader's homemade HTML/CSS reader and a browser.

A row joins the track when the bash RLE wave selector (pipeline/select/bash_rle.py)
stamps it `track: "bash_rle"`. Everything else about the build is the coding
pipeline's: the same gates, controls, review, calibration and release.
"""
from __future__ import annotations

TRACK = "bash_rle"

#: The catalogued levers this wave measured (pipeline/author/levers.py BASH_LEVERS).
LEVERS = ("prior_run_state", "name_spelling_scope", "foreign_saved_files", "parser_hostile_payload",
          "composition_seams", "interrupted_write", "failure_keeps_previous", "label_not_data",
          "invocation_context", "absent_tool_premise")

MANDATE = """\
TRACK: BASH RLE. Arena wants coding environments rich in bash failures: their bash_reliability signal counts \
shell calls that fail for reasons other than a missing prerequisite, beside steerability. Build a task whose \
correctness hinges on shell robustness, extremely hard on BOTH families. What the first wave measured (three \
tasks shipped at 0-1/10 pass@1 per model on Harbor 0.24, 5-19% of each model's shell calls failing):

1. START FROM THE TRACE'S OWN SHELL FAILURES. The TRACE FAILURE DIGEST below and read_failures list every \
command the trace's agent got wrong, what it did next and what the user said next. Those commands are the \
decisions the next model faces: quoting and word splitting, exit status through pipes and set -e, paths \
relative to the script, through a symlink or under spaces, atomic writes and what survives a failure, locks \
and re-runs, processes it starts and stops. Reproduce each one you build on with sandbox_run.

2. THE SETUP MAKES FIRST-REACH COMMANDS FAIL, AND THE INSTRUCTION SAYS SO IN THE USER'S WORDS. A tool the \
trace's agent assumed is absent (the digest's missing programs: zip, pdfinfo, a global npm prefix that needs \
root), files saved on another machine, a previous result that must survive. Every such condition is stated, \
so each failure is the model's and never the image's; the reference passes in the same image (\
absent_tool_premise: the STL packager's machine had no zip, unzip, pdfinfo or pypdf and 12-13% of both \
models' shell calls failed).

3. AIM EVERY TASK AT BOTH FAMILIES. A run scores 0 when it misses any one decision, so a task beats both \
models only with decisions of each kind (failed runs out of 10). Fable: an interruption mid-write \
(interrupted_write, 9; Astra 1), units that work alone but break when combined (composition_seams, 8; Astra 1), \
a run from another directory (invocation_context, 6; Astra 0). Astra: labels and reply shapes it never saw \
(label_not_data, 10; Fable 3), a failure that must keep the previous result (failure_keeps_previous, 7 on \
TextForge; Fable 6 on STL), content that trips a parser (parser_hostile_payload, 5-8). Both: output left by \
an earlier run (prior_run_state) and every spelling of a name (name_spelling_scope), 10 and 10 each; files \
saved by other programs (foreign_saved_files, Fable 6, Astra 8). Plan at least one decision of each family, \
interlocked on hidden items, in quality.lever_plan.

4. HIDDEN CASES ARE REALISTIC VARIANTS OF RULES THE INSTRUCTION PUBLISHES. Notepad and editor saves (BOM, \
CRLF, no final newline, comments, blank lines), a tool's other output mode (sha256sum's escaped records), \
upper, mixed-case and unaccented twins of names, editor backups (name~), paths with spaces, a symlinked \
invocation, leftovers of an earlier run, a kill mid-write, an ordinary reply whose body says the item is \
unknown. Never one famous pitfall as the whole task: pkill -f matching its own shell measured Fable 1.0 on 9 \
of 9 valid runs.

5. GRADE WITH REAL TOOLS IN THE VERIFIER IMAGE: sha256sum, node --check, the real interpreter, the kernel's \
view of files and processes, byte comparison against expected values computed from pristine inputs captured \
before the agent's code runs. Never a homemade reimplementation of a browser, a shell or an OS: the site \
generator's page reader needed 10 review rounds and still disagreed with browsers.

6. NO VOIDS. The shipped grader refuses agent-written source containing /proc/self, /dev/fd, ARENA_ or \
/logs/verifier: publish in the user's words that their security scanner rejects scripts mentioning /proc/self \
or /dev/fd (6 runs of c-001 were voided without it). No clock or timezone traps (Arena asked for none), no \
unpublished numeric bounds, readiness by polling with generous ceilings, kernel-observed facts over a child \
process's own claims, and no rig the agent's process can forge.

7. STEERABILITY. Carry the user's explicit constraints from the trace's corrections into the instruction as \
published rules, in the user's own language and register.

8. THE WORLD OF A BASH TASK IS THE MACHINE'S STATE (THE ENVIRONMENT IS A WORLD, user 2026-10-30). Generate it \
with generate_world: the directories the scripts walk, at realistic scale -- files saved by different programs \
(BOM, CRLF, no final newline, comments, blank lines), names with spaces and case, accent and editor-backup \
twins (name~), the partial and stale outputs of earlier runs, lock and temp files a killed run left, logs the \
tools wrote, the user's real-looking inputs. The bash levers above live in that state: each hidden item is a \
small ordinary-looking part of it. Timestamps in it are plain data: no clock or timezone decision is graded \
(rule 6 overrides the world doctrine's temporal examples)."""


def applies(row: dict | None) -> bool:
    return bool(row) and row.get("track") == TRACK


def lead(row: dict | None, opening: str) -> str:
    """The designer's opening, led by the track's mandate when the row is on the track."""
    return MANDATE + "\n\n" + opening if applies(row) else opening
