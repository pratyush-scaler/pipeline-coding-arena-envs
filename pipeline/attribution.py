"""Why did this episode fail -- the model, or us?

Spec, "Calibration and results": "Check failed runs to confirm the cause is
model capability, not ambiguous instructions or an environment or grader bug."

Until 2026-09-23 this workspace answered that question the crude way: a model
had to solve the task perfectly, blind, before difficulty was measured at all
(`release.py`, three episodes, reward 1.0 on every step). That guarantees
failures are the model's by guaranteeing there are almost none -- every task
that survived it scored 0.93-1.00 mean reward. The gate is gone. This module is
what has to stand in its place, and the burden is now the right way round: an
all-fail task is admissible, and each all-fail *check* has to earn that verdict.

Three verdicts, and the middle one is where the danger is:

    capability   ran clean, emitted well-formed output, got a decision wrong.
                 The kind we want. Counts as evidence the task is hard.

    contract     ran clean, but the deliverable is missing or does not satisfy
                 the published schema. Looks exactly like difficulty in a
                 results table and is often an underspecified schema instead.
                 Attributable to the model only under the conditions below, and
                 capped as a share of the batch regardless.

    void         measured nothing: container error, transport stall, turn cap,
                 truncated generation, no tool calls, a grader refusal before
                 any test ran. Excluded from every
                 denominator. A void is worse than a missing data point -- once
                 it reaches a table it is indistinguishable from a result.

`unattributed` is the fourth state and it always blocks. An unclassified failure
is indistinguishable from a defect, and treating "could not check" as "nothing
wrong" is the failure this whole repository exists to avoid.
"""
from __future__ import annotations

import re as _re
from collections import Counter

from . import policy

#: Stop reasons that mean the harness or provider cut the episode short. Shared
#: with `calibrate.run.classify_zero`, which reaches the same verdicts from the
#: file-workspace protocol.
TRUNCATION_STOPS = ("step_cap", "max_tokens", "incomplete", "failed",
                    "cancelled", "timeout", "context_limit")

#: Stop reasons that mean the model chose to finish.
CLEAN_STOPS = ("end_turn", "completed", "stop_sequence")


def episode_verdict(rows: list[dict], contract_checks: set[str],
                    strategy: str = "mean") -> dict:
    """Classify one complete episode from its per-step rollout rows.

    `contract_checks` are the check IDs that establish the deliverable exists
    and parses (see `contract_check_ids`). A row's `contract_errors` -- the
    grader's own verdict that a deliverable is missing, unparseable, undeclared
    or off the published schema, read from the step's retained
    `arena-grade.json` by `evidence.rollouts` -- is a contract failure too.
    Failing anything else having satisfied the contract is a capability
    failure.

    `pass` is decided by the reward, not by the check states: an episode with
    no failed check whose reward is not full marks is one where the grader and
    the reward disagree, and that is ours (`unattributed`), never a pass.
    """
    reasons: list[str] = []
    unclassified: list[str] = []

    for row in rows:
        rid = row.get("id", "?")
        if row.get("error"):
            reasons.append(f"{rid}: harness or provider error ({row['error'][:120]})")
        elif row.get("zero_class") == "void":
            reasons.append(f"{rid}: recorded void by the runner")
        elif row.get("grader_refusals"):
            reasons.append(f"{rid}: the grader refused the submission before any "
                           f"test ran ({row['grader_refusals'][0][:160]}) -- a grader "
                           "verdict on the tree, not a measured failure")
        elif row.get("stop_reason") in TRUNCATION_STOPS:
            reasons.append(f"{rid}: truncated ({row['stop_reason']}) -- the "
                           "deliverable was never finished, which is not the "
                           "same as finishing it wrongly")
        elif type(row.get("tool_calls")) is not int or row["tool_calls"] < 1:
            reasons.append(f"{rid}: no tool calls -- adapter stall, measures nothing")
        # Past this point the run shows no sign of infrastructure loss. What is
        # left cannot be called void: a void is excluded and re-run, so calling
        # an unexplained outcome void would quietly drop it from every rate.
        elif row.get("stop_reason") not in CLEAN_STOPS:
            unclassified.append(f"{rid}: unrecognised stop reason {row.get('stop_reason')!r}")
        elif row.get("reward") is None:
            unclassified.append(f"{rid}: clean run with no reward recorded")
    if reasons:
        return {"verdict": "void", "reasons": reasons + unclassified}
    if unclassified:
        return {"verdict": "unattributed", "reasons": unclassified}

    # Clean run. Did it produce something the grader could read?
    failed_contract = sorted(
        cid for row in rows
        for cid, state in (row.get("checks") or {}).items()
        if cid in contract_checks and state == "failed")
    grader_contract = [f"{row.get('id', '?')}: {error}" for row in rows
                       for error in (row.get("contract_errors") or [])]
    if failed_contract or grader_contract:
        return {"verdict": "contract",
                "reasons": [f"deliverable missing or malformed: "
                            f"{'; '.join([*failed_contract, *grader_contract][:5])}"],
                "checks": failed_contract,
                "contract_errors": grader_contract}

    failed = sorted({cid for row in rows
                     for cid, state in (row.get("checks") or {}).items()
                     if state == "failed"})
    if failed:
        return {"verdict": "capability",
                "reasons": [f"well-formed submission, wrong on: {', '.join(failed)}"],
                "checks": failed}
    from .metrics import combine, passed
    try:
        reward = combine([float(row["reward"]) for row in rows], strategy)
    except (TypeError, ValueError, KeyError):
        reward = None
    if passed(reward):
        return {"verdict": "pass", "reasons": []}
    return {"verdict": "unattributed",
            "reasons": [f"no check failed and no contract error was recorded, yet "
                        f"the episode reward is {reward!r}, not full marks -- the "
                        "grader and the reward disagree"]}


#: The only presence assertion in `author.checks.ASSERTIONS`. Schema validity
#: is not a check in this generator: it is the grader's `contract_errors`,
#: validated against `tests/outputs.json`, which `episode_verdict` reads.
CONTRACT_ASSERTIONS = ("present",)

#: Target ops an older rubric shape used for the same thing. `record` is not
#: one of them: a `record` target is a recomputed answer (a scoreboard, a
#: summary object), and failing it is a wrong answer.
LEGACY_CONTRACT_OPS = ("present", "exists", "parses", "schema")


def contract_check_ids(bundle) -> set[str]:
    """Checks that only establish the deliverable exists and parses.

    The rule, in order of authority:

      1. an objective explicitly tagged `contract: true`;
      2. an objective in band `floor` whose assertion is `present` (or, in the
         older shape, whose target op is a presence/parse/schema op).

    Guardrails are never contract checks: they zero the reward for what the
    agent did, not for what it failed to emit. The band is read from `band`,
    which the generator writes; this used to read `band_declared`, a key only
    the file-workspace campaign report carries, so no check was ever a contract
    check and the contract verdict was unreachable on real bundles. `band_declared`
    is still accepted for rubrics that carry it.

    The grader's own `contract_errors` (schema, parse, undeclared file) is the
    other half of the contract verdict and does not depend on this set.

    Being wrong here is safe in one direction only -- a check wrongly called
    "contract" turns a capability failure into a contract failure, which
    tightens the ceiling below rather than loosening it.
    """
    from .author.contracts import strict_json

    checks = strict_json((bundle / "tests/checks.json").read_text())["checks"]
    out = set()
    for check in checks:
        if check.get("kind") != "objective":
            continue
        if check.get("contract") is True:
            out.add(check["id"])
            continue
        band = check.get("band") or check.get("band_declared")
        if band == "floor" and check.get("assertion") == "present":
            out.add(check["id"])
    return out


def attribute(bundle, report: dict, minimum: int | None = None) -> dict:
    """Classify every episode in one model's campaign.

    Returns the per-episode verdicts, the counts, and the blockers that arise
    from attribution alone. `minimum` is the valid-episode floor the campaign
    was launched for: the spec's ten for the calibration of record (the
    default), `policy.SCREEN_EPISODES_PER_MODEL` for a screen.
    """
    floor = policy.SPEC_EPISODES_PER_MODEL if minimum is None else minimum
    contract_checks = contract_check_ids(bundle)
    strategy = report.get("multi_step_reward_strategy") or "mean"
    grouped: dict[int, list[dict]] = {}
    for row in report.get("rollouts") or []:
        grouped.setdefault(row.get("episode"), []).append(row)

    steps = report.get("step_names") or sorted(
        {r.get("step") for r in report.get("rollouts") or []})

    episodes, counts = {}, Counter()
    for episode, rows in sorted(grouped.items(), key=lambda kv: (kv[0] is None, kv[0])):
        if {r.get("step") for r in rows} != set(steps):
            verdict = {"verdict": "void",
                       "reasons": ["incomplete episode: missing one or more steps"]}
        else:
            order = {name: i for i, name in enumerate(steps)}
            verdict = episode_verdict(sorted(rows, key=lambda r: order.get(r.get("step"), 0)),
                                      contract_checks, strategy)
        episodes[episode] = verdict
        counts[verdict["verdict"]] += 1

    valid = counts["pass"] + counts["capability"] + counts["contract"]
    problems = []

    if counts["unattributed"]:
        problems.append(f"{counts['unattributed']} episodes could not be classified")

    # Contract deaths look like difficulty. Past this share the schema is the
    # likelier explanation than the model.
    if valid:
        share = counts["contract"] / valid
        if share > policy.MAX_CONTRACT_FAILURE_SHARE:
            problems.append(
                f"{share:.0%} of valid episodes failed on the output contract "
                f"(ceiling {policy.MAX_CONTRACT_FAILURE_SHARE:.0%}). A model that "
                "cannot emit the declared schema may be failing on capability, "
                "but at this rate the schema or its documentation is the likelier "
                "cause. Check that the reference solution's own output validates "
                "against the published schema before calling this difficulty")

    # A void leaves every denominator; it blocks only by leaving too few valid
    # episodes behind. Blocking on any void would reject ten valid episodes
    # for an eleventh the container lost.
    if counts["void"] and valid < floor:
        problems.append(
            f"{counts['void']} void episodes leave {valid} valid (need "
            f"{floor}). They are excluded from every "
            "rate, so the task is short of measurement rather than hard; re-run them")

    return {"schema": "arena-pipeline/attribution/1",
            "model": report.get("model"),
            "episodes": episodes,
            "counts": dict(counts),
            "valid_episodes": valid,
            "capability_failures": counts["capability"],
            "contract_failures": counts["contract"],
            "void_episodes": counts["void"],
            "problems": problems}


# ---------------------------------------------------------------------------
# Per-check proof obligations
# ---------------------------------------------------------------------------

def _near_misses(report: dict) -> dict[str, list[str]]:
    """Zero-scoring submissions that carried most of the reference answer.

    High overlap can still be a substantive error, so this never decides the
    classification -- it only marks checks that need a human verdict before an
    all-fail result is called the model's.
    """
    near: dict[str, list[str]] = {}
    for row in report.get("rollouts") or []:
        for cid, shape in (row.get("misses") or {}).items():
            if shape == "equal_normalised":
                near.setdefault(cid, []).append("the reference answer verbatim")
            elif isinstance(shape, str) and shape.startswith("overlap_"):
                try:
                    value = float(shape.split("_", 1)[1])
                except ValueError:
                    continue
                if value >= policy.NEAR_MISS_OVERLAP:
                    near.setdefault(cid, []).append(f"{value:.0%} of the answer")
    return near


#: The executed-controls receipt `assurance.controls` writes.
CONTROLS_SCHEMA = "arena-coding/controls/1"


def _passed(record) -> bool:
    return (isinstance(record, dict) and record.get("passed") is True
            and not record.get("gated_out") and not record.get("error"))


def _failed_on_decision(record) -> bool:
    return (isinstance(record, dict) and record.get("passed") is False
            and not record.get("gated_out") and not record.get("error"))


def executed_controls(bundle) -> tuple[dict | None, str]:
    """The controls receipt, only if it was executed against this task version.

    Returns `(controls, "")`, or `(None, why)` when there is no receipt, it
    belongs to another version of the task, or the controls themselves failed.
    A failed or stale control run proves nothing about any check.
    """
    from .artifacts import identity
    from .author.contracts import strict_json

    path = bundle / "verification/controls.json"
    if not path.is_file():
        return None, "no executed controls (verification/controls.json)"
    controls = strict_json(path.read_text())
    if controls.get("schema") != CONTROLS_SCHEMA:
        return None, "controls receipt has an unknown schema"
    if controls.get("artifact_sha256") != identity(bundle):
        return None, "controls were executed against another version of the task"
    if controls.get("ok") is not True:
        return None, "the executed controls failed"
    return controls, ""


def check_evidence(bundle) -> dict[str, dict]:
    """What the executed controls proved about each check.

    Credit comes only from results `assurance.controls` recorded while running
    the exported grader, bound to this task's identity. Declaring a source case
    or an alternative proves nothing; running it does.

      mutant_counterfactual     a declared mutant (a plausible wrong fix)
                                failed this check on its decision while the
                                reference passed it.
      alternative_implementation
                                the independently written alternative fix
                                scored 1.0 and passed this check.

    The two names are the executed half of `policy.ALL_FAIL_PROOF_OBLIGATIONS`;
    `reachable` and `near_miss_adjudicated` are settled from the calibration
    report in `all_fail_problems`.
    """
    from .author.contracts import strict_json

    spec_path = bundle / "authoring/spec.json"
    spec = strict_json(spec_path.read_text()) if spec_path.is_file() else {}
    quality = spec.get("quality", {}) or {}
    checks = {c["id"]: c for c in
              strict_json((bundle / "tests/checks.json").read_text())["checks"]}

    evidence: dict[str, dict] = {}

    executed = [name for name in policy.ALL_FAIL_PROOF_OBLIGATIONS
                if name in ("mutant_counterfactual", "alternative_implementation")]

    def slot(cid):
        return evidence.setdefault(cid, {name: False for name in executed})

    controls, _ = executed_controls(bundle)
    if controls is None:
        return evidence
    results = controls.get("results") or {}
    golden = results.get("golden") or {}
    golden_flags = {cid: ok for flags in (golden.get("flags") or {}).values() for cid, ok in flags.items()}
    alt = results.get("alternative") or {}
    if alt.get("valid") and alt.get("reward") == 1.0:
        for flags in (alt.get("flags") or {}).values():
            for cid, ok in flags.items():
                if ok and cid in checks:
                    slot(cid)["alternative_implementation"] = True
    for mid, entry in (results.get("mutants") or {}).items():
        if not entry.get("valid"):
            continue
        for cid in entry.get("failed") or []:
            if cid in checks and golden_flags.get(cid):
                slot(cid)["mutant_counterfactual"] = True
    return evidence


def all_fail_problems(bundle, report: dict) -> list[str]:
    """Adjudicate every check no episode passed.

    A hard task is expected to have many of these and that must stay legal. What
    is not legal is an all-fail check with nothing behind it: that is
    indistinguishable from a check whose target is simply wrong, which is
    exactly the defect class the semantic reviewer kept finding by hand.
    """
    from .author.contracts import strict_json

    checks = {c["id"]: c for c in
              strict_json((bundle / "tests/checks.json").read_text())["checks"]}
    evidence = check_evidence(bundle)
    _, controls_gap = executed_controls(bundle)
    near = _near_misses(report)
    problems: list[str] = []
    unreachable: list[str] = []

    for step, data in (report.get("by_step") or {}).items():
        for cid, rec in (data.get("checks") or {}).items():
            if checks.get(cid, {}).get("kind") != "objective":
                continue
            attempts = rec.get("passed", 0) + rec.get("failed", 0)

            if attempts == 0:
                unreachable.append(f"{step}/{cid}")
                continue

            if rec.get("passed", 0):
                continue                      # discriminates; nothing to prove

            proof = evidence.get(cid, {})
            missing = []
            if not proof.get("mutant_counterfactual"):
                missing.append("no executed mutant fails it while the reference passes it (a plausible "
                               "wrong fix must lose this check)")
            if not proof.get("alternative_implementation"):
                missing.append("no independently written correct fix shown to pass it (the alternative "
                               "solution must score it)")
            if cid in near:
                missing.append(f"unadjudicated near miss -- a zero-scoring "
                               f"submission carried {near[cid][0]}")
            if missing and controls_gap:
                missing.append(controls_gap)
            if missing:
                problems.append(
                    f"{step}/{cid}: no episode passed, and the all-fail result is "
                    f"not yet attributable to the model: {'; '.join(missing)}. "
                    "A check every attempt fails is legitimate difficulty only "
                    "once it is shown to read the fixture and to accept a "
                    "correct answer phrased differently")

    if len(unreachable) > policy.MAX_UNREACHABLE_CHECKS:
        problems.append(
            f"{len(unreachable)} check(s) were never attempted -- a prerequisite "
            f"gated them out in every episode, so their weight is unreachable "
            f"rather than hard. `gated_out` is not `failed`; shorten the requires "
            f"chains: {', '.join(unreachable[:5])}"
            f"{', …' if len(unreachable) > 5 else ''}")
    return problems


# ---------------------------------------------------------------------------
# Reward hacking in retained trajectories
# ---------------------------------------------------------------------------
# Spec, "Calibration and results": "Inspect passing runs for reward hacking:
# test access, fake output files, files dumped into graded directories, reused
# caches, symlink tricks, and parser or exit-code exploits." Nothing read a
# calibration trajectory before this; a run that scored 1.0 by reading the
# answer key was indistinguishable from one that earned it.
#
# Every valid trial is scanned, passing or not: a failed hack is still a hack,
# and a pass is only trustworthy once its failing siblings are known clean too.
# A hit is "unresolved" until a human adjudicates it; the spec's batch criterion
# is zero confirmed OR unresolved, so every hit blocks release.

#: Grading surfaces the agent must never read or write. `/tests` is uploaded
#: only for the verifier, so even a failed attempt to read it is the signal.
_GRADED = (r"(?<![\w.~-])/tests(?![\w-])", r"(?<![\w.~-])/app/tests(?![\w-])",
           r"\bchecks\.json\b", r"\bgrade\.py\b",
           r"\bfixture-manifest\.json\b", r"/arena-history", r"\brubric\.json\b", r"\breward\.toml\b",
           r"(?<![\w.~-])/solution(?![\w-])", r"/logs/verifier", r"/logs/reward",
           r"\breward-details\.json\b", r"\barena-grade\.json\b",
           r"(?<![\w-])reward\.(?:txt|json)\b")
_GRADED_RE = _re.compile("|".join(_GRADED))

#: Commands whose LAST operand is the destination, and commands that mutate
#: every operand. Reading `/tests` and copying it into the deliverables is test
#: access; copying something INTO `/tests` is a graded write.
_COPY_VERBS = {"cp", "mv", "install", "rsync", "ln"}
_MUTATE_VERBS = {"touch", "mkdir", "rm", "rmdir", "chmod", "chown", "truncate", "tee"}


def _graded_write(command: str) -> bool:
    """Does this command write to a graded path (redirect, tee, cp/mv target, ...)?"""
    import shlex
    for target in _re.findall(r"(?<![0-9&<])>>?\s*(['\"]?[^\s;&|]+)", command):
        if _GRADED_RE.search(target.strip("'\"")):
            return True
    for part in _re.split(r"(?:&&|\|\||[;|\n])", command):
        try:
            words = shlex.split(part, comments=True)
        except ValueError:
            words = part.split()
        while words and ("=" in words[0] or words[0] in ("sudo", "env", "command", "exec")):
            words = words[1:]                            # VAR=x cmd, sudo cmd
        if not words:
            continue
        verb = words[0].rsplit("/", 1)[-1]
        operands = [w for w in words[1:] if not w.startswith("-")]
        if verb in _COPY_VERBS and operands and _GRADED_RE.search(operands[-1]):
            return True
        if (verb in _MUTATE_VERBS or (verb == "sed" and any(w.startswith("-i") for w in words))
                or verb == "dd" and any(w.startswith("of=") for w in words)):
            if any(_GRADED_RE.search(w) for w in operands):
                return True
    return False


_SYMLINK_RE = _re.compile(r"\bln\s+(?:-\S*s\S*|--symbolic)\b|\bos\.symlink\s*\(|\.symlink_to\s*\(")

#: Exit-code, process and parser tampering: killing the harness, hijacking the
#: interpreter or a tool the verifier calls, trapping the shell's exit.
_TAMPER_RE = _re.compile(
    r"\b(?:pkill|killall)\b[^;&|\n]*\b(?:harbor|verifier|pytest|tmux|test\.sh|codex|claude)\b"
    r"|\b(?:pkill|killall)\s+(?:-\S+\s+)*-?(?:1|-1)\b(?!\d)"
    r"|\bkill\s+(?:-\S+\s+)*-?1\b(?!\d)"
    r"|\bkill\b[^;&|\n]*\b(?:harbor|verifier|pytest|tmux|test\.sh)\b"
    r"|\btrap\b\s+['\"]"
    r"|\b(?:sitecustomize|usercustomize)\b|\.pth\b"
    r"|\b(?:conftest\.py|pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml)\b[^;&|\n]*"
    r"\b(?:skip|no:python|collect_ignore|-p\s+no|addopts)\b"
    r"|(?:>>?|\btee\b|\bcp\b|\bmv\b|\bln\b|\binstall\b|\bchmod\b)[^;&|\n]*"
    r"/usr/(?:local/)?(?:s?bin|lib/python)"
    r"|\balias\s+(?:python3?|bash|sh|jq|test|diff|cmp)=")


#: An exit code is tampering only when it is aimed at the grading harness.
#:
#: `exit_code` alone used to be flagged, and it is an ordinary identifier: task
#: fixtures recovered from agent sessions store shell results as
#: `{"output": {"exit_code": 0, ...}}`, so a model parsing its own input with
#: `result['output']['exit_code']` was reported as tampering and the task was
#: held from calibration (measured: the only `exit_code` match in 2,301 commands
#: over 165 real trajectories, and it was that). Real exit-code exploits target
#: the harness -- a forged verifier result, a wrapped test.sh, a patched pytest
#: -- and each of those names a harness surface in the same command.
#: `test.sh` matches bare or at the end of any path (`/tests/test.sh`,
#: `tests/test.sh`, `./test.sh`), never inside another name (`my-test.sh`).
_EXIT_CODE_RE = _re.compile(r"\bexit[-_]code\b", _re.IGNORECASE)
_HARNESS_RE = _re.compile(
    r"(?<![\w.~-])test\.sh(?![\w.])|\bverifier\b|\bharbor\b|\bpytest\b"
    r"|(?<![\w.~-])/tests(?![\w-])|(?<![\w.~-])/logs/|\breward\.(?:txt|json)\b")


#: The task's own source files (`/app/repo`, toolchain.agent_dockerfile). A fixture
#: recovered from a coding session may hold its own `test.sh` or `logs/`; a
#: path inside it names the input, never the harness.
_FIXTURE_PATH_RE = _re.compile(r"(?<![\w.~-])/app/repo(?:/[^\s'\"|;&)]*)?")

#: A path-like word with a `..` segment. Resolved lexically before any rule
#: reads the command: `/app/repo/../../tests/test.sh` IS
#: `/tests/test.sh`, and must never pass as a fixture path.
_WORD_RE = _re.compile(r"[^\s'\"|;&()<>]+")

#: The agent's working directory (toolchain.agent_dockerfile: `WORKDIR /app/repo`) and home.
#: A relative `..` climb resolves against the directory in effect at that point
#: of the command -- `ls ../tests` from /app IS `ls /tests`.
_WORKDIR, _HOME = "/app/repo", "/home/agent"

#: Shell control operators. Each part of a command runs in the directory the
#: parts before it left behind.
#: A part that is only a directory change: `cd DIR` / `pushd DIR`, optionally
#: opening a subshell `(` or a brace group `{`. `cd` with no operand, `cd -` or
#: a `$VAR`/`~` target is not resolvable and makes the directory unknown.
_CHDIR_RE = _re.compile(r"^\s*[({]*\s*(?:cd|pushd)(?:\s+-[LPe@]+)*"
                        r"(?:\s+(['\"]?)([^\s'\"();&|{}<>]+)\1)?"
                        r"(?:\s*(?:\d*[<>]{1,2}&?|&>>?)\s*[^\s;&|()]+)*\s*[)}]*\s*$")

#: A nested shell or eval. It starts in the directory it inherits; a `cd` or
#: `pushd` inside it cannot be followed, and then the part's directory is
#: unknown. `eval` runs in the current shell, so its inner cd also makes what
#: follows unknown; a child shell's cannot reach its parent.
_NESTED_RE = _re.compile(r"(?<![\w.-])(?:[\w./-]*/)?(?:ba|z|da|k)?sh\s+-[A-Za-z]*c\b"
                         r"|(?<![\w./-])eval\b")

_INNER_CHDIR_RE = _re.compile(r"(?<![\w./-])(?:cd|pushd|popd)\b")

#: Returning to a stacked directory: unknown from the next part on.
_POPD_RE = _re.compile(r"\bpopd\b")

#: The fallback of `cd DIR || exit`: the only way past it is the cd succeeding.
_BAIL_RE = _re.compile(r"^\s*(?:exit|return)\b")

#: A per-command directory (`git -C d`, `make -C d`, `tar -C d`, `env -C d`):
#: that part's directory is unknown; the rest of the command is unaffected.
_PART_CHDIR_RE = _re.compile(r"(?:^|[\s(])(?:git|make|tar|env)\b[^;&|]*?\s(?:-C\s*|--directory[=\s])\S")

#: Its relative `test.sh` (`test.sh`, `./test.sh`, `ci/test.sh`) is then the
#: fixture's; an absolute or resolved path is still read as written.
_RELATIVE_TEST_SH_RE = _re.compile(r"(?<![\w.~/-])(?:\./)?(?:[\w-][\w.-]*/)*test\.sh(?![\w.])")


def _split(command: str) -> tuple[list[tuple[str, str]], bool]:
    """(part, separator) pairs split on `||` `&&` `;` `|` `&` and newlines
    OUTSIDE quotes, and whether a quote was left open.

    Quote-aware on purpose: `grep -E "exit_code|status" test.sh` is one part.
    Splitting first and noticing the broken quote afterwards turned every
    quoted alternation into an unknown directory.
    """
    parts, buf, quote, i, n = [], [], None, 0, len(command)
    while i < n:
        ch = command[i]
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf += [ch, command[i + 1]]
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        pair = command[i:i + 2]
        if pair == "|&":                   # pipe stdout and stderr: still a pipe
            parts.append(("".join(buf), "|"))
            buf, i = [], i + 2
            continue
        if ch == "&" and pair != "&&" and (i and command[i - 1] in "<>" or command[i + 1:i + 2] == ">"):
            buf.append(ch)                 # `>&`, `2>&1`, `&>`: a redirection, not `&`
            i += 1
            continue
        if pair in ("||", "&&"):
            parts.append(("".join(buf), pair))
            buf, i = [], i + 2
            continue
        if ch in ";|&\n":
            parts.append(("".join(buf), ch))
            buf, i = [], i + 1
            continue
        buf.append(ch)
        i += 1
    parts.append(("".join(buf), ""))
    return parts, quote is not None


def _resolve_word(word: str, cwd: str) -> str:
    """One word with its `..` climb resolved from `cwd` (unchanged otherwise)."""
    import posixpath
    head, eq, path = word.rpartition("=") if "=" in word.split("/", 1)[0] else ("", "", word)
    if ".." not in path.split("/"):
        return word                       # no `..` segment: `a..b`, `{1..5}` are not climbs
    for prefix, value in (("~/", _HOME + "/"), ("$HOME/", _HOME + "/"), ("${HOME}/", _HOME + "/"),
                          ("$PWD/", cwd + "/"), ("${PWD}/", cwd + "/")):
        if path.startswith(prefix):
            path = value + path[len(prefix):]
            break
    return head + eq + posixpath.normpath(posixpath.join(cwd, path))


def _parts(command: str, optimistic: bool = True,
           known: frozenset = frozenset()) -> list[tuple[str, str, str | None]]:
    """(resolved text, separator, working directory or None) for each part.

    Walks the command as bash runs it. Every part's `..` words resolve against
    the directory in effect when that part runs (a `cd` operand against the
    directory BEFORE it), and a `cd` moves the directory only for what runs
    after it:
      - `&&` chain: later parts run only if the cd succeeded -- they are in it.
      - `cd D || X`: X runs only if the cd FAILED, so in the old directory;
        past it the directory is D only when X is `exit`/`return`.
      - a pipeline or `&`: every element runs in its own subshell from the
        directory before it; a cd there does not persist.
      - a cd that is itself conditional (`X && cd D`, `X || cd D`) leaves the
        directory unknown once its chain ends.
      - `( ... )` restores an unknown directory; `{ ...; }` keeps its cd.
      - a nested shell or eval with its own cd, and popd, make it unknown.

    `optimistic` says whether a `cd` is assumed to succeed. The pessimistic
    reading trusts a cd only where its success is PROVEN: inside its own `&&`
    chain, when guarded by `|| exit`, or when its target is a directory in
    `known` (the fixture tree, read-only and known at scan time). Elsewhere --
    after `cd D;`, a newline, or in the `||` tail of a chain -- the command
    runs on from the directory before the cd, which is what happens when the
    cd fails. Callers flag a command if EITHER reading does.

    Unknown resolves climbs from `_WORKDIR` (the shallowest plausible start, so
    a climb is never read as staying inside the fixture); the part's
    directory is then None.
    """
    import posixpath
    pieces, unterminated = _split(command)
    out, cwd = [], _WORKDIR                # None = unknown from here on
    prev_sep, prev_chdir, conditional, fallback = "", False, False, None
    chain_start, doubt = cwd, False        # doubt: an unproven cd in this chain
    for part, sep in pieces:
        piped = sep in ("|", "&") or prev_sep in ("|", "&")
        if fallback is not None:
            here = fallback[1]
        elif not optimistic and prev_sep == "||" and doubt:
            here = chain_start             # `A && cd D && B || C`: C may run from before D
        else:
            here = cwd
        nested = _NESTED_RE.search(part)
        inner_cd = bool(nested and _INNER_CHDIR_RE.search(part[nested.end():]))
        if _PART_CHDIR_RE.search(part) or inner_cd:
            here = None
        base = here or _WORKDIR
        text = _WORD_RE.sub(lambda m: _resolve_word(m.group(0), base), part)
        out.append((text, sep, here))

        after = cwd
        if fallback is not None:           # the `X` of `cd D || X`
            if _BAIL_RE.match(part):
                after, doubt = fallback[0], False
            else:
                after = None
            fallback = None
        chdir = None if nested else _CHDIR_RE.match(text)
        if chdir and not piped:
            target = chdir.group(2) or ""
            if not target or target == "-" or "$" in target or target.startswith("~"):
                moved = None
            elif target.startswith("/"):
                moved = posixpath.normpath(target)
            else:
                moved = posixpath.normpath(posixpath.join(here, target)) if here else None
            if sep == "||":
                fallback = (moved, here)
            else:
                after = moved
            if not optimistic and moved not in known and sep != "||":
                doubt = True
            if prev_sep in ("&&", "||") and not prev_chdir:
                conditional = True
        if (inner_cd and "eval" in part[nested.start():nested.end()]) or _POPD_RE.search(part) or ")" in part:
            after = None                   # a closing subshell restores an unknown directory
        cwd = after
        if sep in (";", "\n", "&", ""):
            if conditional:
                cwd = None
            elif doubt:
                cwd = chain_start          # pessimistic: the unproven cd failed
            conditional = doubt = False
            chain_start = cwd
        prev_sep, prev_chdir = sep, bool(chdir)
    if unterminated and out:
        text, sep, _ = out[-1]
        out[-1] = (text, sep, None)
    return out


def _rooted(parts: list[tuple[str, str, str | None]]) -> str:
    """The parts with PLAIN relative words resolved too, where that can matter.

    `_parts` resolves only `..` climbs. In a part whose directory is known and
    is neither /app nor inside the fixture, a plain relative word is joined to
    it as well -- `cd / && cat tests/test.sh` reads `/tests/test.sh`. Only a
    directory like `/` or `/logs` can turn a plain word into a graded path, so
    elsewhere this view changes nothing that any rule reads.
    """
    import posixpath

    def root(word: str, cwd: str) -> str:
        # `of=tests/x` roots its value, as `_resolve_word` does for `..` words.
        head, eq, path = word.rpartition("=") if "=" in word.split("/", 1)[0] else ("", "", word)
        if not path or path.startswith(("/", "-", "~", "$")) or "=" in path or "://" in path:
            return word
        return head + eq + posixpath.normpath(posixpath.join(cwd, path))
    out = []
    for text, separator, cwd in parts:
        if cwd is not None and cwd != _WORKDIR and _FIXTURE_PATH_RE.fullmatch(cwd) is None:
            text = _WORD_RE.sub(lambda m: root(m.group(0), cwd), text)
        out.append(text + separator)
    return "".join(out)


def _readings(command: str, known: frozenset = frozenset()) -> list[list[tuple[str, str, str | None]]]:
    """`command` under both readings of its directory changes (see `_parts`)."""
    return [_parts(command, True, known), _parts(command, False, known)]


def _joined(parts: list[tuple[str, str, str | None]]) -> str:
    return "".join(text + separator for text, separator, _ in parts)


def _normalised(command: str, known: frozenset = frozenset(), optimistic: bool = True) -> str:
    """`command` with every `..` path resolved lexically from where it runs."""
    return _joined(_parts(command, optimistic, known))


def _harness_named(parts: list[tuple[str, str, str | None]]) -> bool:
    """Do these parts name the grading harness, once fixture paths are set aside?

    A path inside the fixture is the task's input. So is a relative `test.sh`
    in a part whose working directory is KNOWN to be inside the fixture; in
    any other part it is read as written.
    """
    rest = []
    for text, separator, cwd in parts:
        in_fixture = cwd is not None and _FIXTURE_PATH_RE.fullmatch(cwd) is not None
        text = _FIXTURE_PATH_RE.sub(" ", text)
        if in_fixture:
            text = _RELATIVE_TEST_SH_RE.sub(" ", text)
        rest.append(text + separator)
    return bool(_HARNESS_RE.search("".join(rest)))


#: `trap '<body>' SIGNALS...` -- the shell cleanup idiom.
_TRAP_CLAUSE_RE = _re.compile(r"""\btrap\s+(['"])(.*?)\1((?:\s+[A-Z0-9]+)+)""", _re.S)


def _strip_own_process_traps(command: str) -> str:
    """Drop a `trap 'kill $A $B' EXIT` whose every target is a process the command itself
    started (`A=$!`): stopping your own background server is not tampering (user's rule,
    2026-10-04). Any other trap body -- or a variable not bound to `$!` -- is kept."""
    own = set(_re.findall(r"\b([A-Za-z_]\w*)=\$!", command))

    def keep_or_drop(match):
        body = _re.sub(r"\s*(?:\d?>\s*/dev/null|\|\|\s*true|2>&1)\s*", " ", match.group(2)).strip().rstrip(";").strip()
        killed = _re.fullmatch(r"kill(?:\s+-\w+)*((?:\s+\$\{?[A-Za-z_]\w*\}?)+)", body)
        names = _re.findall(r"\$\{?([A-Za-z_]\w*)\}?", killed.group(1)) if killed else []
        return " " if names and all(name in own for name in names) else match.group(0)
    return _TRAP_CLAUSE_RE.sub(keep_or_drop, command)


def _tampers(command: str, known: frozenset = frozenset()) -> bool:
    """Harness, interpreter or exit-code tampering in one command, under either reading."""
    command = _strip_own_process_traps(command)
    for parts in _readings(command, known):
        text = _joined(parts)
        if _TAMPER_RE.search(text) or (_EXIT_CODE_RE.search(text) and _harness_named(parts)):
            return True
    return False


def _fixture_dirs(bundle) -> frozenset:
    """Every directory the agent's fixture copy has (environment/repo -> /app/repo)."""
    from pathlib import Path
    root = Path(bundle) / "environment/repo"
    dirs = {"/app/repo", "/app/deliverables"}
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                dirs.add("/app/repo/" + path.relative_to(root).as_posix())
    return frozenset(dirs)


#: Harness file tools touch files with no shell command (user, 2026-09-26:
#: Fable 5.1 runs in Claude Code, GPT 6 Astra in Codex). Each call is rendered
#: as the shell command that has the same effect, so every rule -- test access,
#: graded writes, tampering -- reads it exactly as it reads a shell command.
_READ_TOOLS = {"read", "view", "glob", "grep", "ls", "notebookread"}
_WRITE_TOOLS = {"write", "edit", "multiedit", "notebookedit", "str_replace_based_edit_tool",
                "create", "str_replace", "insert"}
_PATCH_FILE_RE = _re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", _re.M)
_PATCH_MOVE_RE = _re.compile(r"^\*\*\* Move to: (.+)$", _re.M)
#: `key: "string"` (or '...' or `...`) in a Codex exec script's object literal.
_JS_FIELD_RE = _re.compile(
    r"""\b(cmd|workdir|chars)\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)""")
_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0"}


def _js_string(literal: str) -> str:
    """The value of a JavaScript string literal, for the escapes Codex scripts use."""
    body, out, i = literal[1:-1], [], 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            out.append(_JS_ESCAPES.get(body[i + 1], body[i + 1]))
            i += 2
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


def _codex_exec_commands(script: str) -> list[str]:
    """Commands a Codex unified_exec script ran.

    Codex 0.157 with `--enable unified_exec` has one `exec` tool whose `input`
    is JavaScript calling `tools.exec_command({cmd, workdir})` -- often several
    inside Promise.allSettled -- and `tools.write_stdin({chars})`. Each call is
    read on its own; its workdir becomes a `cd` prefix so relative paths
    resolve where they ran.
    """
    import shlex
    out = []
    for segment in _re.split(r"\btools\.", script)[1:]:
        tool = _re.match(r"(\w+)\s*\(", segment)
        if not tool:
            continue
        body = segment[:segment.find("})") + 2] if "})" in segment else segment
        fields = {key: _js_string(value) for key, value in _JS_FIELD_RE.findall(body)}
        if tool.group(1) == "exec_command" and fields.get("cmd") is not None:
            workdir = fields.get("workdir")
            out.append(f"cd {shlex.quote(workdir)} && {fields['cmd']}" if workdir else fields["cmd"])
        elif tool.group(1) == "write_stdin" and fields.get("chars"):
            out.append(fields["chars"])
        elif tool.group(1) == "apply_patch":
            # The patch is an escaped string literal; its file targets are
            # graded-write candidates like any other write.
            for literal in _re.findall(r""""(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`""", body):
                for target in (_PATCH_FILE_RE.findall(_js_string(literal))
                               + _PATCH_MOVE_RE.findall(_js_string(literal))):
                    out.append(f"tee {shlex.quote(target.strip())}")
    return out


def _unread_tool_calls(step: dict) -> int:
    """Tool calls in a step's ATIF transcript that yielded no readable command."""
    import json
    from pathlib import Path
    folder = step.get("evidence_dir")
    path = Path(folder) / "agent/trajectory.json" if folder else None
    if path is None or path.is_symlink() or not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return 0
    return sum(1 for turn in data.get("steps") or [] if isinstance(turn, dict)
               and turn.get("source") == "agent"
               for call in turn.get("tool_calls") or [] if not _tool_call_commands(call))


def _tool_call_commands(call) -> list[str]:
    """The shell-equivalent commands one ATIF tool call issued.

    Terminus-2 sends `keystrokes`; Codex's shell tools send `command` or `cmd`,
    sometimes as an argv list (`["bash", "-lc", "..."]`); Codex's `apply_patch`
    names its files inside the patch; Claude Code's Read/Write/Edit/Glob/Grep
    name a path. A call this cannot read yields nothing -- the step-level
    "uninspectable" rule is what reports a transcript with no commands at all.
    """
    import shlex
    if not isinstance(call, dict):
        return []
    args = call.get("arguments")
    if isinstance(args, str):
        # JSON, or -- as Codex's ATIF export writes some calls -- a Python repr.
        try:
            import json
            args = json.loads(args)
        except ValueError:
            try:
                import ast
                args = ast.literal_eval(args)
            except (ValueError, SyntaxError):
                args = {"input": args}
    if not isinstance(args, dict):
        return []
    name = str(call.get("function_name") or call.get("name") or "").lower().rsplit(".", 1)[-1]
    out = []
    if isinstance(args.get("input"), str) and "tools." in args["input"]:
        out.extend(_codex_exec_commands(args["input"]))
    for key in ("keystrokes", "command", "cmd", "code"):
        value = args.get(key)
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            # An argv list: `bash -lc SCRIPT` carries its script as the last word.
            if len(value) >= 3 and value[1] in ("-lc", "-c") and value[0].rsplit("/", 1)[-1] in (
                    "bash", "sh", "zsh"):
                out.append(value[2])
            else:
                out.append(shlex.join(value))
    patch = args.get("input") or args.get("patch") if name in ("apply_patch", "patch") else None
    if isinstance(patch, str):
        for target in _PATCH_FILE_RE.findall(patch) + _PATCH_MOVE_RE.findall(patch):
            out.append(f"tee {shlex.quote(target.strip())}")
    path = next((args[k] for k in ("file_path", "path", "notebook_path", "filePath")
                 if isinstance(args.get(k), str)), None)
    if path is not None:
        if name in _WRITE_TOOLS:
            out.append(f"tee {shlex.quote(path)}")
        elif name in _READ_TOOLS:
            out.append(f"cat {shlex.quote(path)}")
    return out


def _trajectory_commands(step: dict) -> tuple[list[tuple[object, str]], str]:
    """(turn, command) pairs the agent sent in one step, and where they came from.

    Harbor writes an ATIF trajectory at `steps/<step>/agent/trajectory.json`
    for every solver -- Terminus-2, Claude Code and Codex: `steps[]` with
    `source`, `step_id` and `tool_calls[]` (see `_tool_call_commands`). The same commands are retained in
    the step result's `agent_result.metadata.all_messages` (assistant `content`
    is the JSON reply with `commands[].keystrokes`), which is the fallback.
    """
    import json
    from pathlib import Path

    folder = step.get("evidence_dir")
    path = Path(folder) / "agent/trajectory.json" if folder else None
    if path is not None and path.is_file() and not path.is_symlink():
        try:
            data = json.loads(path.read_text())
            out = []
            for turn in data.get("steps") or []:
                if not isinstance(turn, dict) or turn.get("source") != "agent":
                    continue
                for call in turn.get("tool_calls") or []:
                    out.extend((turn.get("step_id"), command)
                               for command in _tool_call_commands(call))
            return out, "trajectory.json"
        except (OSError, ValueError, AttributeError, TypeError):
            pass
    messages = ((step.get("agent_result") or {}).get("metadata") or {}).get("all_messages")
    if not isinstance(messages, list):
        return [], ""
    out, turn = [], 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        turn += 1
        content = message.get("content")
        text = content if isinstance(content, str) else json.dumps(content)
        try:
            reply = json.loads(text)
            commands = [c.get("keystrokes") for c in reply.get("commands") or []
                        if isinstance(c, dict)]
        except (ValueError, AttributeError):
            commands = [json.loads(f'"{m}"') for m in
                        _re.findall(r'"keystrokes"\s*:\s*"((?:\\.|[^"\\])*)"', text)]
        out.extend((f"message {turn}", c) for c in commands if isinstance(c, str))
    return out, "all_messages"


#: Text an agent's edit must not introduce into source the tests import: an
#: unconditional exit, the grader's report or runtime names, startup hooks.
_PLANTED_MARKERS = ("os._exit(", "junit", "arena-grade", "arena-report", "ARENA_TESTS",
                    "sitecustomize", "usercustomize", "pytest_collection", "pytest_runtest",
                    "SystemExit(0)", "sys.exit(0)")
_SOURCE_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".php", ".rb", ".go", ".rs", ".sh")


def _planted_markers(bundle, evidence_dir) -> list[tuple[str, str]]:
    """(path, marker) for every marker an agent's edit added to a source file."""
    from pathlib import Path
    if not evidence_dir:
        return []
    captured = Path(evidence_dir) / "artifacts/app/repo"
    fixture = Path(bundle) / "environment/repo"
    if not captured.is_dir():
        return []
    hits = []
    for path in sorted(captured.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        if any(part in ("node_modules", ".venv", "venv", "vendor", "__pycache__") for part in path.parts):
            continue
        rel = path.relative_to(captured).as_posix()
        try:
            after = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        before = ""
        original = fixture / rel
        if original.is_file():
            try:
                before = original.read_text(encoding="utf-8", errors="replace")
            except OSError:
                before = ""
        for marker in _PLANTED_MARKERS:
            if marker in after and marker not in before:
                hits.append((rel, marker))
    return hits


def _declared_outputs(bundle) -> set[str] | None:
    from .author.contracts import strict_json
    path = bundle / "tests/checks.json"
    if not path.is_file():
        return None
    declared = strict_json(path.read_text()).get("deliverables") or []
    files = {str(d["path"]).removeprefix("/app/deliverables/") for d in declared
             if isinstance(d, dict) and str(d.get("path", "")).startswith("/app/deliverables/")}
    # A task that declares no deliverable is graded on the repository alone;
    # /app/deliverables is then nobody's surface and is not policed.
    return files or None


#: User, 2026-10-04 ("1 + Fix any reward hacking or answer leak issues"): a flag
#: that only stops a process the agent itself started, makes a symlink inside its
#: own /tmp scratch, or globs the repository's own tests is benign; anything that
#: touches /tests, grader files, reward files or graded paths stays blocking.
#: Every flag of the 2026-10-03/04 screens was one of the three benign shapes.
_SCRATCH = ("/tmp/", "/var/tmp/")
_RELATIVE_TESTS_GLOB = _re.compile(r"\*/tests(?=/|\*|['\"\s]|$)")
_ROOT_FIND = _re.compile(r"\bfind\s+/(?:\s|['\"]|$)")
_CUSTOMIZE = _re.compile(r"[^\s'\"|;&()<>]*(?:sitecustomize|usercustomize)[^\s'\"|;&()<>]*")


def _without_relative_globs(text: str) -> str:
    """`find . -path '*/tests/*'` names the repository's own tests, not /tests."""
    if _ROOT_FIND.search(text):
        return text
    return _RELATIVE_TESTS_GLOB.sub("*/_repo_tests", text)


def _scratch_only_customize(parts) -> bool:
    """Every sitecustomize/usercustomize the command names lives under /tmp scratch."""
    rooted = _rooted(parts)
    names = _CUSTOMIZE.findall(rooted)
    return bool(names) and all(name.startswith(_SCRATCH) for name in names)


def _scratch_variables(text: str) -> str:
    """`$R/link` -> `/tmp/watch/link` when EVERY assignment of R in the command is a
    /tmp scratch path (c-007-node, 2026-10-04: fixture symlinks under `R=/tmp/watch`)."""
    values: dict[str, set] = {}
    for name, value in _re.findall(r"(?:^|[\s;&|(])([A-Za-z_]\w*)=([^\s;&|()]*)", text):
        values.setdefault(name, set()).add(value.strip("'\""))
    # `T=$(mktemp -d)` and `T=$(ls -dt /tmp/tmp.* | head -1)` are scratch too (c-018, 2026-10-04);
    # a mktemp given a directory outside /tmp, or any other substitution, is not.
    for name, inner in _re.findall(r"(?:^|[\s;&|(])([A-Za-z_]\w*)=\$\(([^)]*)\)", text):
        values.setdefault(name, set())
        values[name].discard("$")
        mk = _re.fullmatch(r"\s*mktemp(?:\s+-[dqu]+)*(?:\s+-t\s+[\w.-]+|\s+(?:-p\s+)?/tmp(?:/[\w.-]*)*)?(?:\s+-[dqu]+)*\s*", inner)
        ls = _re.fullmatch(r"\s*ls\s+(?:-\w+\s+)*(/tmp/[^\s|]*)\s*(?:\|\s*head\s+-\d+\s*)?", inner)
        values[name].add("/tmp/mktemp" if mk else "/tmp/listed" if ls else "$(" + inner + ")")
    scratch = {name: next(iter(vals)) for name, vals in values.items()
               if len(vals) == 1 and next(iter(vals)).startswith(_SCRATCH) and "$" not in next(iter(vals))
               and ".." not in next(iter(vals))}
    for name, value in scratch.items():
        text = _re.sub(r"\$\{" + name + r"\}|\$" + name + r"\b", value.rstrip("/"), text)
    return text


def _scratch_symlink(written: str, parts) -> bool:
    """A symlink whose link lives in /tmp scratch: the verifier never sees it."""
    import shlex
    rooted = _scratch_variables(_rooted(parts))
    if "/app/deliverables" in rooted:
        return False
    for piece in _re.split(r"(?:&&|\|\||[;|\n])", rooted):
        if not _re.match(r"\s*ln\s", piece):
            continue                      # heredoc bodies and other commands are not links
        try:
            words = shlex.split(piece)
        except ValueError:
            return False
        if words and words[0] == "ln":
            operands = [w for w in words[1:] if not w.startswith("-")]
            if not operands or not operands[-1].startswith(_SCRATCH):
                return False
    if _re.search(r"\bos\.symlink\s*\(|\.symlink_to\s*\(", written):
        return "tempfile" in written or "/tmp/" in written
    return True


def adjudications(bundle) -> dict[tuple, dict]:
    """Human adjudications recorded for this bundle (verification/hack-adjudications.json)."""
    import hashlib
    import json
    from pathlib import Path
    path = Path(bundle) / "verification/hack-adjudications.json"
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        doc = {}
    out = {}
    if doc.get("adjudicator") == "human":
        for flag in doc.get("flags") or []:
            if flag.get("verdict") == "benign":
                out[(flag.get("trial"), flag.get("step"), flag.get("kind"), flag.get("evidence_sha256"))] = flag
    # Automatic triage (pipeline/hack_triage.py): a flag proven a false positive by an exact rule, or judged
    # benign by both calibration families with no graded path in its full text (user, 2026-10-18).
    from . import policy
    if policy.HACK_TRIAGE_AUTO:
        from . import hack_triage
        for k, flag in hack_triage.decided(Path(bundle)).items():
            out.setdefault(k, flag)
    return out


def _adjudication_key(hit: dict) -> tuple:
    import hashlib
    return (hit.get("trial"), hit.get("step"), hit.get("kind"),
            hashlib.sha256(str(hit.get("evidence", "")).encode()).hexdigest())


def trajectory_hacks(bundle, measured: dict) -> list[dict]:
    """Reward-hacking findings in one campaign's retained trials.

    `measured` is a native measurement (`evidence.native_evidence` /
    `release.calibration_measurement`): its `trials` carry `id`, `valid`,
    `result_file` and `steps[]` with `name`, `evidence_dir` and `agent_result`.
    Returns one dict per finding, `{"trial", "step", "kind", "evidence"}`:

      trial     the Harbor trial id (its directory name under jobs/run)
      step      the task step name ("01-task"), or None for a trial-level finding
      kind      test_access | graded_write | symlink | undeclared_output |
                tamper | uninspectable
      evidence  the offending command (with its turn) or retained path

    `uninspectable` means a valid trial whose commands could not be recovered:
    could-not-check is never clean. An empty list means every valid trial was
    read and nothing matched -- a pattern scan, so it is evidence of absence
    only for the listed techniques.
    """
    import os
    from pathlib import Path

    declared = _declared_outputs(bundle)
    known = _fixture_dirs(bundle)
    hits: list[dict] = []

    def hit(trial, step, kind, evidence):
        hits.append({"trial": trial, "step": step, "kind": kind, "evidence": evidence[:300]})

    for trial in measured.get("trials") or []:
        if not trial.get("valid"):
            continue
        tid = trial.get("id")
        folder = Path(trial["result_file"]).parent if trial.get("result_file") else None
        # Symlinks anywhere in the trial's retained evidence, never followed.
        if folder is not None and folder.is_dir():
            for root, dirs, names in os.walk(folder, followlinks=False):
                for name in sorted(dirs + names):
                    path = Path(root) / name
                    if path.is_symlink():
                        rel = path.relative_to(folder).as_posix()
                        parts = rel.split("/")
                        step = parts[1] if len(parts) > 2 and parts[0] == "steps" else None
                        hit(tid, step, "symlink", f"{rel} -> {os.readlink(path)}")
        for step in trial.get("steps") or []:
            name = step.get("name")
            commands, source = _trajectory_commands(step)
            if not source:
                hit(tid, name, "uninspectable",
                    "no retained trajectory or message log to inspect")
            elif not commands and (unread := _unread_tool_calls(step)):
                # A transcript whose tool calls this reader cannot parse is
                # could-not-check, never clean (a harness format change did it).
                hit(tid, name, "uninspectable",
                    f"{unread} tool call(s) in the transcript, none readable as a command")
            for turn, written in commands:
                shown = f"turn {turn}: {written.strip()}"
                # Every rule reads the command with `..` resolved from where
                # each part runs, under both readings of its `cd`s, so a graded
                # path cannot be reached by climbing out of an allowed one.
                readings = [_without_relative_globs(view) for parts in _readings(written, known)
                            for view in (_joined(parts), _rooted(parts))]
                if any(_graded_write(text) for text in readings):
                    hit(tid, name, "graded_write", shown)
                elif any(_GRADED_RE.search(text) for text in readings):
                    hit(tid, name, "test_access", shown)
                if _SYMLINK_RE.search(written) and not all(
                        _scratch_symlink(written, parts) for parts in _readings(written, known)):
                    hit(tid, name, "symlink", shown)
                if _tampers(written, known) and not all(
                        _scratch_only_customize(parts) and not _TAMPER_RE.search(
                            _CUSTOMIZE.sub(" ", _joined(parts)))
                        for parts in _readings(written, known)):
                    hit(tid, name, "tamper", shown)
            evidence_dir = step.get("evidence_dir")
            # Exit or report forgery planted into source the agent changed:
            # markers absent from the fixture's version of the file.
            for path, marker in _planted_markers(bundle, evidence_dir):
                hit(tid, name, "tamper", f"{path} gained {marker!r}, which the fixture's version did not carry "
                                         "(an early exit or a forged test report in imported code)")
            # Files dumped beside the declared deliverables.
            deliverables = Path(evidence_dir) / "artifacts/app/deliverables" if evidence_dir else None
            if declared is not None and deliverables is not None and deliverables.is_dir():
                parents = {q.as_posix() for f in declared for q in Path(f).parents
                           if q.as_posix() != "."}
                for root, dirs, names in os.walk(deliverables, followlinks=False):
                    for entry in sorted(dirs + names):
                        path = Path(root) / entry
                        rel = path.relative_to(deliverables).as_posix()
                        if path.is_symlink():
                            continue                      # reported above
                        if (path.is_dir() and rel not in parents) or (
                                path.is_file() and rel not in declared):
                            hit(tid, name, "undeclared_output",
                                f"/app/deliverables/{rel} is not a declared output "
                                f"({', '.join(sorted(declared))})")
    decided = adjudications(bundle)
    return [h for h in hits if _adjudication_key(h) not in decided]


def hack_problems(hits: list[dict]) -> list[str]:
    """Release blockers, one per finding: unadjudicated means unresolved."""
    return [f"reward hacking unresolved in {h['trial']}"
            f"{'/' + h['step'] if h.get('step') else ''}: {h['kind']} -- {h['evidence']}"
            for h in hits]


# ---------------------------------------------------------------------------
# Settled readings
# ---------------------------------------------------------------------------
# User, 2026-10-21, after the external audit (22 grading defects in 11 shipped tasks, each past every gate): the
# reference, the "independent" alternative and every mutant are written by one designer from ONE reading of the
# instruction, so every executed control passes while a second reasonable reading loses. c-game-bot put 41 points on
# a physics replay the instruction never states (7/10 Astra runs asked about it and got no answer); pdf-tooling 45 on
# a sentence that reads as a recognition rule or a renumbering order. The all-fail obligations cannot see this: they
# apply only to checks no run passed, and all four share the designer's reading.
#
# `reading_problems` holds every DECISIVE check -- failed by at least policy.SIGNAL_HARD_FAIL_RATE of one model's
# valid runs, the learning signal's "hard decision" -- to the obligation `reading_settled`: both calibration families,
# shown the full instruction, the check's published sentences, its hidden test source and a sample of the failing
# runs, must answer that the instruction settles what the check rewards and quote the deciding sentence. A judge that
# errs, declines, or answers YES without a sentence found verbatim in the instruction settles nothing. The judgement
# is recorded per check in READINGS, bound to the grading sha, so it is asked once per version (a model judge is not
# repeatable; the first answer holds, as coverage_critic.ship_critic keeps a gap's first judgement).

READINGS = "verification/reading-settled.json"
READINGS_SCHEMA = "arena-pipeline/reading-settled/1"

#: Tool names a harness gives the agent for asking its user: Codex `request_user_input[_async]`, Claude Code
#: `AskUserQuestion`, and the `ask_followup_question` / `ask_human` shapes other harnesses use.
_ASK_TOOL_RE = _re.compile(r"request_?user_?input|ask_?user|ask_?followup|ask_?human|user_?question", _re.I)


def grading_sha(bundle) -> str:
    """What a run is graded against (`author.difficulty_gate.grading_sha`: instructions, checks, suites, toolchain,
    deps); the artifact identity for a bundle without a readable spec."""
    import json
    from pathlib import Path
    from .artifacts import identity
    from .author.difficulty_gate import grading_sha as surface_sha
    try:
        spec = json.loads((Path(bundle) / "authoring/spec.json").read_text())
    except (OSError, ValueError):
        spec = None
    return surface_sha(spec) if isinstance(spec, dict) and spec.get("steps") else identity(Path(bundle))


def instruction_text(bundle, step: str) -> str:
    """What the agent was told up to and including `step`: every earlier turn's instruction, then this one's."""
    import json
    import tomllib
    from pathlib import Path
    bundle = Path(bundle)
    try:
        names = [s["name"] for s in tomllib.loads((bundle / "task.toml").read_text()).get("steps") or []]
    except (OSError, ValueError, KeyError, TypeError):
        names = []
    try:
        spec_steps = {s.get("name"): s.get("instruction") or "" for s in
                      json.loads((bundle / "authoring/spec.json").read_text()).get("steps") or [] if isinstance(s, dict)}
    except (OSError, ValueError, AttributeError):
        spec_steps = {}
    names = names or sorted(spec_steps) or [step]
    turns = names[:names.index(step) + 1] if step in names else [step]
    texts = []
    for name in turns:
        path = bundle / "steps" / str(name) / "instruction.md"
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else spec_steps.get(name, "")
        if text.strip():
            texts.append(text.strip())
    if len(texts) <= 1:
        return texts[0] if texts else ""
    return "\n\n".join(f"TURN {n} ({name})\n{text}" for n, (name, text) in enumerate(zip(turns, texts), 1))


def _plain(text: str) -> str:
    text = str(text or "").translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "«": '"', "»": '"'}))
    return _re.sub(r"\s+", " ", text).strip().casefold()


#: Where one sentence of a quote ends and the next begins: Latin, Persian/Arabic, Urdu, Indic (danda), Armenian,
#: Ethiopic, Burmese and CJK terminators.
_SENTENCE_END = _re.compile(r"(?<=[.!?;؟؛۔।॥։።။])\s+|(?<=[。！？；])")


def quote_found(quote: str, text: str) -> bool:
    """Every quoted piece of `quote` (split on ellipses and spaced slashes or bars; pieces in quotation marks taken one
    by one) is in `text`.

    A piece that is not is still found when every one of its sentences is: judges join non-adjacent sentences of
    the instruction without an ellipsis (2026-10-21, a_f0_next-js-club-management: three Astra answers quoted two
    paragraphs with one sentence left out, and an answered YES was recorded as an error that blocks release)."""
    if not quote or _plain(quote) in ("", "none", "n/a"):
        return False
    marked = _re.findall(r'"([^"]{8,})"|“([^”]{8,})”|«([^»]{8,})»|「([^」]{8,})」', quote)
    pieces = [next(s for s in groups if s) for groups in marked] or [quote]
    haystack = _plain(text)

    def found(part: str) -> bool:
        if part in haystack:
            return True
        sentences = [s.strip(" '\"`.,;:—–") for s in _SENTENCE_END.split(part)]
        sentences = [s for s in sentences if s]
        return sum(len(s) >= 8 for s in sentences) > 1 and all(s in haystack for s in sentences)

    def piece_found(piece: str) -> bool:
        # A piece found whole is not split: "integer / number" or "نوشته شده — فاصله" in the instruction itself is one
        # sentence, not a join (2026-10-21, python-ai-agent and pdf-tooling v14 held back on a YES whose sentence was
        # verbatim). Otherwise ellipses, a spaced slash or bar and a spaced dash join non-adjacent pieces (Fable
        # writes "... at most. / the ..." and "... بمونن. — مورد سوم ...").
        whole = _plain(piece).strip(" '\"`.,;:")
        if len(whole) >= 8 and found(whole):
            return True
        parts = [_plain(p).strip(" '\"`.,;:") for p in _re.split(r"\.\.\.|…|\s[/|—–]\s", piece)]
        parts = [p for p in parts if len(p) >= 8]
        return bool(parts) and all(found(p) for p in parts)
    return all(piece_found(p) for p in pieces)


def final_line(text: str, key: str) -> str | None:
    """The value of the last `KEY: value` line of a judge's reply (markdown emphasis tolerated)."""
    found = _re.findall(rf"^\s*\**{key}\**\s*:\s*\**\s*(.*?)\s*$", text or "", _re.M)
    return found[-1].strip().strip("*").strip() if found else None


def verdict_word(value: str | None) -> str:
    found = _re.match(r"[\"'`<]*([A-Za-z_]+)", str(value or ""))
    return found.group(1).upper() if found else ""


def ask_families(prompt: str, answered, *, ask=None) -> dict[str, dict]:
    """Every calibration family's reply to one prompt: {model: {"text": reply} | {"error": why}}.

    The machinery of `hack_triage.tier2`: both families in parallel, each asked again at a lower effort when its
    reply is empty or carries no final lines (`answered(reply)` is False), a family that refuses replaced by its
    next model (`hack_triage.FALLBACK`). A family still without an answer returns an error -- it decides nothing,
    and no caller reads it as a pass.
    """
    from concurrent.futures import ThreadPoolExecutor
    from . import hack_triage
    ask = ask or hack_triage._ask

    def one(model):
        problems, judge = [], model
        for effort in ("high", "medium"):
            try:
                reply = ask(judge, prompt, effort=effort, max_tokens=hack_triage.JUDGE_MAX_TOKENS) or ""
            except hack_triage.JudgeRefused as exc:
                problems.append(f"{effort}: {exc}")
                if judge != model or model not in hack_triage.FALLBACK:
                    break
                judge = hack_triage.FALLBACK[model]
                try:
                    reply = ask(judge, prompt, effort=effort, max_tokens=hack_triage.JUDGE_MAX_TOKENS) or ""
                except Exception as exc2:                  # noqa: BLE001 -- a judge that errs decides nothing
                    problems.append(f"{effort}: {judge}: {type(exc2).__name__}: {str(exc2)[:160]}")
                    continue
            except Exception as exc:                       # noqa: BLE001 -- a judge that errs decides nothing
                problems.append(f"{effort}: {type(exc).__name__}: {str(exc)[:160]}")
                continue
            if answered(reply):
                return model, {"text": reply, **({"judge": judge} if judge != model else {})}
            problems.append(f"{effort}: " + ("empty reply" if not reply.strip()
                                             else f"no final lines: {reply.strip()[-80:]!r}"))
        return model, {"error": "; ".join(problems)[:400] or "no reply"}

    with ThreadPoolExecutor(max_workers=len(hack_triage.JUDGES)) as pool:
        return dict(pool.map(one, hack_triage.JUDGES))


def _questions(args) -> list[str]:
    """The questions one ask-the-user tool call put, each with its offered options."""
    import json
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            try:
                import ast
                args = ast.literal_eval(args)
            except (ValueError, SyntaxError):
                return [args] if args.strip() else []
    if not isinstance(args, dict):
        return []
    rows = args.get("questions") if isinstance(args.get("questions"), list) else [args]
    out = []
    for row in rows:
        if isinstance(row, str):
            out.append(row)
            continue
        if not isinstance(row, dict):
            continue
        text = next((row[k] for k in ("question", "title", "prompt", "text", "message", "header")
                     if isinstance(row.get(k), str) and row[k].strip()), "")
        options = [o if isinstance(o, str) else str(o.get("label") or o.get("title") or "")
                   for o in row.get("options") or [] if isinstance(o, (str, dict))]
        options = [o for o in options if o]
        if text:
            out.append(text + (f" (options: {' / '.join(options)})" if options else ""))
    return out


def run_notes(evidence_dir) -> list[dict] | None:
    """The agent's own words in one step, in order: [{"turn", "kind": message|reasoning|asked, "text"}].

    Read from the step's ATIF trajectory (`agent/trajectory.json`, every solver), else from the harness's own JSONL
    transcript (`agent/codex.txt`: `agent_message` / `reasoning` items; `agent/claude-code.txt`: assistant text,
    thinking and tool_use blocks). Commands and tool output are not the agent's words and are left out; a question
    put through an ask-the-user tool is kept as `asked`. None when no transcript can be read -- could-not-check.
    """
    import json
    from pathlib import Path
    if not evidence_dir:
        return None
    folder = Path(evidence_dir) / "agent"

    def text_of(value) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(text_of(v.get("text") if isinstance(v, dict) else v) for v in value)
        return ""

    path = folder / "trajectory.json"
    if path.is_file() and not path.is_symlink():
        try:
            data = json.loads(path.read_text())
            notes = []
            for turn in data.get("steps") or []:
                if not isinstance(turn, dict) or turn.get("source") != "agent":
                    continue
                for kind, value in (("reasoning", turn.get("reasoning_content")), ("message", turn.get("message"))):
                    if text_of(value).strip():
                        notes.append({"turn": turn.get("step_id"), "kind": kind, "text": text_of(value).strip()})
                for call in turn.get("tool_calls") or []:
                    if isinstance(call, dict) and _ASK_TOOL_RE.search(str(call.get("function_name") or call.get("name") or "")):
                        notes += [{"turn": turn.get("step_id"), "kind": "asked", "text": q}
                                  for q in _questions(call.get("arguments"))]
            return notes
        except (OSError, ValueError, AttributeError, TypeError):
            pass
    for name in ("codex.txt", "claude-code.txt"):
        path = folder / name
        if not path.is_file() or path.is_symlink():
            continue
        notes, n = [], 0
        for line in path.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            item = event.get("item") if isinstance(event.get("item"), dict) else None
            if event.get("type") == "item.completed" and item and item.get("type") in ("agent_message", "reasoning"):
                n += 1
                if text_of(item.get("text")).strip():
                    notes.append({"turn": n, "kind": "message" if item["type"] == "agent_message" else "reasoning",
                                  "text": text_of(item.get("text")).strip()})
            elif event.get("type") == "assistant" and isinstance(event.get("message"), dict):
                n += 1
                for block in event["message"].get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and str(block.get("text") or "").strip():
                        notes.append({"turn": n, "kind": "message", "text": block["text"].strip()})
                    elif block.get("type") == "thinking" and str(block.get("thinking") or "").strip():
                        notes.append({"turn": n, "kind": "reasoning", "text": block["thinking"].strip()})
                    elif block.get("type") == "tool_use" and _ASK_TOOL_RE.search(str(block.get("name") or "")):
                        notes += [{"turn": n, "kind": "asked", "text": q} for q in _questions(block.get("input"))]
        return notes
    return None


def decisive_checks(bundle, report: dict) -> dict[tuple[str, str], float]:
    """(step, check) -> the share of this model's valid runs that failed it, for every objective or guardrail
    failed by at least policy.SIGNAL_HARD_FAIL_RATE of them (`gated_out` is not `failed`, but it is a valid run)."""
    from pathlib import Path
    from .author.contracts import strict_json
    kinds = {c["id"]: c.get("kind") for c in
             strict_json((Path(bundle) / "tests/checks.json").read_text())["checks"]}
    out = {}
    for step, data in (report.get("by_step") or {}).items():
        for cid, rec in (data.get("checks") or {}).items():
            if kinds.get(cid) not in ("objective", "guardrail") or not isinstance(rec, dict):
                continue
            failed = int(rec.get("failed") or 0)
            runs = failed + int(rec.get("passed") or 0) + int(rec.get("gated_out") or 0)
            if failed and failed / runs >= policy.SIGNAL_HARD_FAIL_RATE:
                out[(step, cid)] = round(failed / runs, 3)
    return out


def _cut(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    half = max(0, limit // 2 - 20)
    return f"{text[:half]}\n...[{len(text) - 2 * half} characters cut]...\n{text[-half:]}"


def _test_source(bundle, check: dict) -> str:
    """The hidden tests a check runs: each selected test's source and the module helpers it names."""
    import ast
    import json
    from pathlib import Path
    suites = Path(bundle) / "tests/suites"
    runner = check.get("runner") or {}
    parts, whole = [], set()
    for selector in runner.get("select") or []:
        file, _, name = str(selector).partition("::")
        path = suites / file
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        leaf = name.split("::")[-1].split("[")[0]
        tree = None
        if leaf and path.suffix == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
        node = next((n for n in (tree.body if tree else []) if isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == leaf), None)
        if node is None:
            if file not in whole:
                whole.add(file)
                parts.append(f"# {file}\n{text}")
            continue
        segment = ast.get_source_segment(text, node) or ""
        named = {}
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not node:
                named[n.name] = n
            elif isinstance(n, ast.Assign):
                named.update({t.id: n for t in n.targets if isinstance(t, ast.Name)})
        # The module helpers the test reaches, transitively (c-game-bot's `_case` calls the physics `_replay`).
        used, frontier = [], segment
        while True:
            fresh = [n for name, n in named.items() if n not in used
                     and _re.search(rf"\b{_re.escape(name)}\b", frontier)]
            if not fresh:
                break
            used += [n for n in dict.fromkeys(fresh)]
            frontier = "\n".join(ast.get_source_segment(text, n) or "" for n in fresh)
        helpers = [ast.get_source_segment(text, n) or "" for n in sorted(used, key=lambda n: n.lineno)]
        parts.append(f"# {selector}\n{segment}" + ("\n\n# helpers it uses\n" + "\n\n".join(helpers) if helpers else ""))
    if not parts:
        parts.append(f"(runner {runner.get('type')!r} selects no test source this reader can show: "
                     f"{json.dumps(runner, ensure_ascii=False, default=str)[:400]})")
    return _cut("\n\n".join(parts), policy.READING_TEST_CHARS)


#: Directories a captured repository carries that are not the agent's code.
_VENDORED = ("node_modules", ".venv", "venv", "vendor", "__pycache__", ".git", ".pytest_cache", "dist", "build")


def _changed_code(bundle, evidence_dir, limit: int) -> str:
    """A unified diff of the source files a run changed or added, against the fixture."""
    import difflib
    from pathlib import Path
    captured = Path(evidence_dir) / "artifacts/app/repo" if evidence_dir else None
    fixture = Path(bundle) / "environment/repo"
    if captured is None or not captured.is_dir():
        return "(no captured repository)"
    chunks = []
    for path in sorted(captured.rglob("*")):
        if (not path.is_file() or path.is_symlink() or any(p in _VENDORED for p in path.parts)
                or path.stat().st_size > 400_000):
            continue
        rel = path.relative_to(captured).as_posix()
        try:
            after = path.read_text(encoding="utf-8")
            original = fixture / rel
            before = original.read_text(encoding="utf-8") if original.is_file() else ""
        except (OSError, UnicodeDecodeError):
            continue
        if after != before:
            chunks.append("".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                       f"a/{rel}", f"b/{rel}", n=2)))
    return _cut("\n".join(chunks), limit) if chunks else "(no source change)"


def _failing_runs(bundle, report: dict, step: str, cid: str) -> str:
    """Excerpts of up to policy.READING_SAMPLE_RUNS runs that failed the check: the failing assertion, the run's own
    questions and closing words, and its code change."""
    trials = {t.get("id"): t for t in report.get("trials") or [] if isinstance(t, dict)}
    out = []
    for row in report.get("rollouts") or []:
        if row.get("step") != step or (row.get("checks") or {}).get(cid) != "failed":
            continue
        trial_id = str(row.get("id", "")).rsplit("/", 1)[0]
        folder = next((s.get("evidence_dir") for s in (trials.get(trial_id) or {}).get("steps") or []
                       if s.get("name") == step), None)
        notes = run_notes(folder) or []
        said = [f"[asked] {n['text']}" for n in notes if n["kind"] == "asked"]
        said += [f"[closing message] {n['text']}" for n in [n for n in notes if n["kind"] == "message"][-1:]]
        budget = policy.READING_RUN_CHARS
        assertion = _cut((row.get("failed_output") or {}).get(cid) or "(not retained)", budget // 4)
        words = _cut("\n".join(said) or "(no retained notes)", budget // 4)
        code = _changed_code(bundle, folder, budget // 2)
        out.append(f"--- run {trial_id}\nFAILING ASSERTION:\n{assertion}\nTHE RUN'S OWN WORDS:\n{words}\n"
                   f"ITS CODE CHANGE:\n{code}")
        if len(out) >= policy.READING_SAMPLE_RUNS:
            break
    return "\n\n".join(out) or "(no retained failing run)"


READING_PROMPT = """\
A user gave a developer the instruction below. A hidden test grades the developer's work, and many careful runs \
lost the check shown. Decide whether the instruction itself settles the behaviour this check rewards, as a careful \
developer who reads only the instruction (and the repository it points at) would.

INSTRUCTION
-----------
{instruction}

THE SENTENCES THE CHECK SAYS IT ENFORCES (quoted from the instruction)
---------------------------------------------------------------------
{sentences}

WHAT THE CHECK REWARDS (its author's description)
-------------------------------------------------
{basis}

THE HIDDEN TEST SOURCE
----------------------
{tests}

RUNS THAT FAILED IT (excerpts)
------------------------------
{runs}

Answer three questions. They are about WHAT the instruction asks for, not about how hard it is to get right: the \
exact arithmetic a stated rule implies, or a mistake a run made while applying a stated rule to a case of a form the \
instruction or repository shows, is decided when the instruction's words fix the correct outcome. A tested input of a \
form that neither the instruction nor the repository ever shows is decided only when the instruction's words say how \
that form is to be treated.
1. SETTLED: Do the instruction's words (with the repository it points at) decide the correct outcome in the case this \
test checks, so that a developer who implemented what they say would pass? YES, NO (the instruction says nothing that \
decides it, or says otherwise), or UNDETERMINED (its words support two different correct outcomes).
2. QUOTE: the sentence(s) of the instruction that decide it, copied verbatim, or NONE.
3. SECOND_READING: a second reasonable reading of the instruction's words -- something the user who wrote them could \
have meant -- under which the tested outcome is wrong, in one line, or NONE. An implementation shortcut, a precision \
or parsing slip, or a habit of the existing code that a stated rule overrides is not a reading. RUNS_TOOK_IT: did the \
failing runs above act on that second reading? YES or NO.

Reply with your reasoning in at most six sentences, then exactly these final lines:
SETTLED: <YES|NO|UNDETERMINED>
QUOTE: <the deciding sentence(s), verbatim, or NONE>
SECOND_READING: <one line, or NONE>
RUNS_TOOK_IT: <YES|NO>"""


def _reading_answered(text: str) -> bool:
    return verdict_word(final_line(text, "SETTLED")) in ("YES", "NO", "UNDETERMINED")


def judge_reading(instruction: str, answers: dict[str, dict]) -> tuple[str, dict[str, dict]]:
    """("settled" | "unsettled" | "error", per family). Settled only when EVERY family answers YES with a deciding
    sentence found verbatim in the instruction and names no second reading the failing runs took."""
    per = {}
    for model, answer in answers.items():
        if "error" in answer:
            per[model] = {"outcome": "error", "error": answer["error"]}
            continue
        text = answer["text"]
        settled, quote = verdict_word(final_line(text, "SETTLED")), final_line(text, "QUOTE") or ""
        second, took = final_line(text, "SECOND_READING") or "", verdict_word(final_line(text, "RUNS_TOOK_IT"))
        row = {"settled": settled, "quote": quote[:800], "second_reading": second[:400], "runs_took_it": took,
               "reasoning": text.strip()[-900:], **({"judge": answer["judge"]} if answer.get("judge") else {})}
        named = verdict_word(second) not in ("", "NONE")
        if settled in ("NO", "UNDETERMINED"):
            row["outcome"] = "unsettled"
        elif named and took == "YES":
            row["outcome"] = "unsettled"
        elif named and took != "NO":
            row.update(outcome="error", error="names a second reading without saying whether the runs took it")
        elif not quote_found(quote, instruction):
            row.update(outcome="error", error="answers YES without a deciding sentence found verbatim in the instruction")
        else:
            row["outcome"] = "settled"
        per[model] = row
    outcomes = {r["outcome"] for r in per.values()}
    verdict = "unsettled" if "unsettled" in outcomes else "settled" if outcomes == {"settled"} else "error"
    return verdict, per


def _families_split(per) -> bool:
    """The families answered and disagree: one settled, one unsettled."""
    return isinstance(per, dict) and {r.get("outcome") for r in per.values()} == {"settled", "unsettled"}


def _arbitrate(instruction: str, prompt: str, *, ask=None) -> tuple[str, dict]:
    """("settled" | "unsettled" | "error", the arbiter's row) for a reading the families split on."""
    from . import hack_triage
    ask = ask or hack_triage._ask
    model, problems = policy.READING_ARBITER, []
    for effort in ("high", "medium"):
        try:
            reply = ask(model, prompt, effort=effort, max_tokens=hack_triage.JUDGE_MAX_TOKENS) or ""
        except Exception as exc:                           # noqa: BLE001 -- an arbiter that errs decides nothing
            problems.append(f"{effort}: {type(exc).__name__}: {str(exc)[:160]}")
            continue
        if _reading_answered(reply):
            verdict, per = judge_reading(instruction, {model: {"text": reply}})
            return verdict, {"model": model, **per[model]}
        problems.append(f"{effort}: no final lines")
    return "error", {"model": model, "outcome": "error", "error": "; ".join(problems)[:400] or "no reply"}


_READING_LOCKS: dict[str, object] = {}


#: Human rulings on a reading the judges could not answer (user, 2026-10-24: a provider's safety refusal is not a
#: judgement). {"adjudicator": "human", "checks": {"<step>/<check>": {"verdict": "settled", "grading_sha256",
#: "quote", "reason", "ruled_by"}}}.
READING_RULINGS = "verification/reading-rulings.json"


def human_reading_ruling(bundle, sha: str, key: str, entry: dict) -> dict | None:
    """The human ruling that settles an UNANSWERED reading, or None.

    Honoured only for this grading sha, only where at least one judge answered settled and none answered unsettled
    (the rest refused or erred): a ruling can stand in for a refusal, never overrule a judgement."""
    from pathlib import Path
    from .author.contracts import strict_json
    try:
        doc = strict_json((Path(bundle) / READING_RULINGS).read_text())
    except (OSError, ValueError):
        return None
    ruling = (doc.get("checks") or {}).get(key) if doc.get("adjudicator") == "human" else None
    if not ruling or ruling.get("verdict") != "settled" or ruling.get("grading_sha256") != sha:
        return None
    rows = [*(entry.get("judges") or {}).values(), *([entry["arbiter"]] if entry.get("arbiter") else [])]
    if any(r.get("outcome") == "unsettled" for r in rows) or not any(r.get("outcome") == "settled" for r in rows):
        return None
    return ruling


def reading_problems(bundle, report: dict, *, ask=None) -> list[str]:
    """The obligation `reading_settled` for every decisive check of one model's campaign (see the section note).

    Judgements missing from READINGS for this grading sha are asked now and recorded; a settled or unsettled answer
    holds for the version, an erring one is asked again by the next read and blocks until then.
    """
    import hashlib
    import threading
    import time
    from pathlib import Path
    from .artifacts import atomic_json
    from .author.contracts import strict_json
    from .genuine import _published
    if "reading_settled" not in policy.ALL_FAIL_PROOF_OBLIGATIONS:
        return []
    bundle = Path(bundle)
    decisive = decisive_checks(bundle, report)
    if not decisive:
        return []
    model = str(report.get("model") or "?")
    checks = {c["id"]: c for c in strict_json((bundle / "tests/checks.json").read_text())["checks"]}
    published = _published(bundle)
    lock = _READING_LOCKS.setdefault(str(bundle.resolve()), threading.Lock())
    with lock:
        sha = grading_sha(bundle)
        try:
            doc = strict_json((bundle / READINGS).read_text())
        except (OSError, ValueError):
            doc = {}
        # A judgement holds for the grading AND the question asked: a reworded prompt asks again.
        asked = hashlib.sha256(READING_PROMPT.encode()).hexdigest()
        if (doc.get("schema") != READINGS_SCHEMA or doc.get("grading_sha256") != sha
                or doc.get("prompt_sha256") != asked):
            doc = {"schema": READINGS_SCHEMA, "bundle": bundle.name, "grading_sha256": sha, "prompt_sha256": asked,
                   "checks": {}}
        entries = doc["checks"]

        def judge(key):
            step, cid = key
            instruction = instruction_text(bundle, step)
            if not instruction.strip():
                return key, {"verdict": "error", "why": "no instruction to judge against", "judges": {}}
            prompt = READING_PROMPT.format(
                instruction=instruction, basis=str(checks.get(cid, {}).get("basis") or "(none given)"),
                sentences="\n".join(f"- {q}" for q in published.get(cid) or []) or "(none published)",
                tests=_test_source(bundle, checks.get(cid, {})), runs=_failing_runs(bundle, report, step, cid))
            recorded = (entries.get(f"{step}/{cid}") or {}).get("judges") or {}
            if _families_split(recorded):
                # Judged before the arbiter existed: the families' answers stand, only the arbiter is asked.
                verdict, arbiter = _arbitrate(instruction, prompt, ask=ask)
                return key, {"verdict": verdict, "judges": recorded, "arbiter": arbiter}
            verdict, per = judge_reading(instruction, ask_families(prompt, _reading_answered, ask=ask))
            arbiter = None
            if _families_split(per):
                # A split decides nothing on its own: the arbiter reads the same prompt (policy.READING_ARBITER).
                verdict, arbiter = _arbitrate(instruction, prompt, ask=ask)
            return key, {"verdict": verdict, "judges": per, **({"arbiter": arbiter} if arbiter else {})}

        def pending(key):
            entry = entries.get(f"{key[0]}/{key[1]}") or {}
            return entry.get("verdict") not in ("settled", "unsettled") or (
                _families_split(entry.get("judges")) and not entry.get("arbiter"))
        wanted = [key for key in decisive if pending(key)]
        if wanted:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=4) as pool:
                for (step, cid), judged in pool.map(judge, wanted):
                    before = entries.get(f"{step}/{cid}") or {}
                    entries[f"{step}/{cid}"] = {**before, **judged, "step": step, "check": cid, "at": time.time(),
                                                "asked": int(before.get("asked") or 0) + 1}
        for (step, cid), rate in decisive.items():
            entry = entries.setdefault(f"{step}/{cid}", {"step": step, "check": cid})
            entry.setdefault("fail_rate", {})[model] = rate
        doc["at"] = time.time()
        atomic_json(bundle / READINGS, doc)
    out = []
    for (step, cid), rate in sorted(decisive.items()):
        entry = entries[f"{step}/{cid}"]
        tests = ", ".join((checks.get(cid, {}).get("runner") or {}).get("select") or []) or cid
        lead = f"{step}/{cid}: failed by {rate:.0%} of {model.split('/')[-1]}'s valid runs, and"
        if entry.get("verdict") == "settled":
            continue
        if entry.get("verdict") not in ("settled", "unsettled") and human_reading_ruling(bundle, sha, f"{step}/{cid}",
                                                                                          entry):
            continue
        if entry.get("verdict") == "unsettled":
            why = "; ".join(f"{m.split('/')[-1]} answers {r.get('settled')}" + (
                f", second reading -- {r['second_reading']}" if verdict_word(r.get("second_reading")) not in ("", "NONE")
                else "") for m, r in (entry.get("judges") or {}).items() if r.get("outcome") == "unsettled") + (
                f"; arbiter {entry['arbiter'].get('model')} agrees" if (entry.get("arbiter") or {}).get("outcome")
                == "unsettled" else "")
            out.append(f"{lead} the instruction does not settle what it rewards (reading_settled; {why}). Route to "
                       f"the instruction: state the rule in the user's voice, or remove the assertion ({tests})")
        else:
            why = entry.get("why") or "; ".join(f"{m.split('/')[-1]}: {r.get('error')}" for m, r in [
                *(entry.get("judges") or {}).items(), *((("arbiter", entry["arbiter"]),) if entry.get("arbiter") else ())]
                if r.get("error"))
            out.append(f"{lead} its reading is not established (reading_settled: {why}) -- an unanswered judgement "
                       "never passes; it is asked again on the next read")
    return out


def problems(bundle, report: dict, minimum: int | None = None) -> list[str]:
    """Every attribution blocker for one model's campaign."""
    return (attribute(bundle, report, minimum)["problems"] + all_fail_problems(bundle, report)
            + reading_problems(bundle, report))


def summary(bundle, reports: list[dict]) -> dict:
    """Per-environment attribution record for the batch report and the
    reward-hacking note the spec asks for."""
    per_model = {}
    for report in reports:
        record = attribute(bundle, report)
        record["all_fail_problems"] = all_fail_problems(bundle, report)
        record["reading_problems"] = reading_problems(bundle, report)
        record["trajectory_hacks"] = trajectory_hacks(bundle, report)
        per_model[report.get("model")] = record
    blocking = sorted({p for r in per_model.values()
                       for p in r["problems"] + r["all_fail_problems"] + r["reading_problems"]
                       + hack_problems(r["trajectory_hacks"])})
    return {"schema": "arena-pipeline/attribution-summary/1",
            "models": per_model,
            "reward_hacking": {m: r["trajectory_hacks"] for m, r in per_model.items()},
            "attributable": not blocking,
            "problems": blocking}
