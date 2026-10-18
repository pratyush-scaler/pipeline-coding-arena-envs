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


def problems(bundle, report: dict, minimum: int | None = None) -> list[str]:
    """Every attribution blocker for one model's campaign."""
    return attribute(bundle, report, minimum)["problems"] + all_fail_problems(bundle, report)


def summary(bundle, reports: list[dict]) -> dict:
    """Per-environment attribution record for the batch report and the
    reward-hacking note the spec asks for."""
    per_model = {}
    for report in reports:
        record = attribute(bundle, report)
        record["all_fail_problems"] = all_fail_problems(bundle, report)
        record["trajectory_hacks"] = trajectory_hacks(bundle, report)
        per_model[report.get("model")] = record
    blocking = sorted({p for r in per_model.values()
                       for p in r["problems"] + r["all_fail_problems"]
                       + hack_problems(r["trajectory_hacks"])})
    return {"schema": "arena-pipeline/attribution-summary/1",
            "models": per_model,
            "reward_hacking": {m: r["trajectory_hacks"] for m, r in per_model.items()},
            "attributable": not blocking,
            "problems": blocking}
