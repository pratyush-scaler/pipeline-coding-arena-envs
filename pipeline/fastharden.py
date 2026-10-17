"""Hidden-only hardening, measured in minutes on the episodes already run.

User, 2026-10-04: "harden tasks in minutes rather than in hours ... while still
making the task extremely extremely hard for both the models", and every
failure "a genuine model incapability failure" with no grader/env/infra issue.

A full hardening attempt is a redesign: hours of designer time, then a fresh
5 x 2 screen to learn whether it worked. Most of that waiting is the screen.
But a screen keeps every episode's final repository
(`steps/<s>/artifacts/app`), and an agent's behaviour depends only on what it
can see -- the instruction and the fixture. A revision that changes NOTHING the
agent sees, only hidden tests, can therefore be measured against those trees
at once: `sandbox.grade_capture` re-grades each in seconds.

The protocol (analysis/web-research/W6-minute-scale-hardening.md):

  * hidden-only: step instructions, the fixture and the declared deliverables
    stay byte-identical (`hidden_only_problems`); only suites, checks, traps,
    mutants and the reference/alternative patches may change;
  * the designer sees which hidden checks every captured episode passes, can
    read what an episode changed (`read_episode`), and re-grades its draft
    against all of them (`regrade_episodes`) -- every failure classified by
    `genuine.classify`, so a case that fails by crashing the suite is visible
    as such, not as difficulty;
  * every new case must be settled by a published sentence and pass the
    determinacy probe (`author.determinacy`): both calibration families, asked
    directly, give the answer the reference computes;
  * the re-graded rate is IN-SAMPLE (the cases were chosen with those trees in
    view) and is reported only as `provisional`; the revision then takes the
    normal validity path (controls, gates, review) and a FRESH screen, which is
    the only number that counts, followed by `genuine.audit`.
"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import policy

#: Designer turns for a hidden-only round: a bounded edit, not a redesign. 90 until 2026-10-15: two gap
#: rounds (club-management, nfse) spent all 90 on piecemeal stage_spec edits and read_draft (89 + 57 and
#: 52 + 32 calls) and never submitted.
FAST_MAX_STEPS = 150
REGRADE_WORKERS = 6
RECEIPT = "verification/fast-regrade.json"


def captured(bundle: Path) -> list[dict]:
    """The valid episodes of the bundle's current screen, with their captured trees."""
    from .genuine import episodes
    out = []
    for episode in episodes(Path(bundle)):
        if not episode["valid"] or episode["verdict"] == "void":
            continue
        steps = []
        for step in episode["steps"]:
            folder = Path(step["evidence_dir"]) if step.get("evidence_dir") else None
            capture = folder / "artifacts/app" if folder else None
            if capture is None or not (capture / "repo").is_dir():
                steps = []
                break
            steps.append({"name": step["name"], "capture": str(capture)})
        if steps:
            out.append({"model": episode["model"], "id": episode["id"], "steps": steps})
    return out


def _classified(package: Path, step: str, capture: Path, cid: str, rec: dict, suites: set[str], tag: str) -> dict:
    """`genuine.classify`, re-running a silent runner (node-test) so its reason can be read."""
    from .genuine import classify, explain
    kind, detail = classify(rec, suites)
    if kind == "suspect" and detail.startswith("the test produced no output"):
        try:
            text = explain(package, step, capture, cid, verifier_tag=tag)
        except Exception:                                 # noqa: BLE001 -- stays suspect
            text = None
        if text:
            kind, detail = classify({**rec, "stdout": text, "stderr": ""}, suites)
    return {"kind": kind, "detail": detail}


def regrade(package: Path, episodes: list[dict], *, workers: int = REGRADE_WORKERS) -> dict:
    """Grade every captured episode with `package`'s verifier; per-model, per-step summary."""
    from .author import sandbox
    from .genuine import classify, _suites
    package = Path(package)
    suites = _suites(package)
    tag = sandbox.build_image(package, "verifier")

    def one(episode):
        history, steps = {}, {}
        for step in episode["steps"]:
            capture = Path(step["capture"])
            try:
                result = sandbox.grade_capture(package, step["name"], capture, history=dict(history),
                                               verifier_tag=tag)
            except Exception as exc:                      # noqa: BLE001 -- infrastructure, reported
                steps[step["name"]] = {"valid": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
                break
            history[step["name"]] = capture
            grade = result.get("grade") or {}
            failed = {cid: rec for cid, rec in (grade.get("checks") or {}).items()
                      if isinstance(rec, dict) and not rec.get("passed") and not rec.get("gated_out")}
            steps[step["name"]] = {
                "valid": bool(result.get("valid")), "reward": result.get("reward"),
                "full": result.get("reward") == 1.0,
                "contract_errors": grade.get("contract_errors") or [],
                "failed": {cid: _classified(package, step["name"], capture, cid, rec, suites, tag)
                           for cid, rec in failed.items()},
                "error": result.get("error")}
        return {"model": episode["model"], "id": episode["id"], "steps": steps}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        rows = list(pool.map(one, episodes))
    summary: dict = {"schema": "arena-coding/fast-regrade/1", "in_sample": True, "episodes": rows,
                     "by_model": {}, "problems": []}
    for row in rows:
        model = summary["by_model"].setdefault(row["model"], {"episodes": 0, "steps": {}, "checks": {}})
        model["episodes"] += 1
        for name, step in row["steps"].items():
            cell = model["steps"].setdefault(name, {"graded": 0, "full": 0})
            if not step.get("valid"):
                summary["problems"].append(f"{row['model']} {row['id']} {name}: not graded "
                                           f"({step.get('error') or 'invalid verifier result'})")
                continue
            cell["graded"] += 1
            cell["full"] += int(step["full"])
            for cid, failure in step["failed"].items():
                stats = model["checks"].setdefault(f"{name}/{cid}", {"failed": 0, "kinds": {}})
                stats["failed"] += 1
                stats["kinds"][failure["kind"]] = stats["kinds"].get(failure["kind"], 0) + 1
                if failure["kind"] not in ("assertion", "repo_crash", "output"):
                    summary["problems"].append(f"{row['model']} {row['id']} {name}/{cid}: {failure['kind']} "
                                               f"-- {failure['detail'][:200]}")
    for model in summary["by_model"].values():
        for cell in model["steps"].values():
            cell["provisional_pass_rate"] = round(cell["full"] / cell["graded"], 3) if cell["graded"] else None
    limit = policy.STEP_PASS_RATE_MAX
    summary["provisional_meets_bar"] = all(
        cell["provisional_pass_rate"] is not None and cell["provisional_pass_rate"] <= limit
        for model in summary["by_model"].values() for cell in model["steps"].values())
    return summary


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


#: Spec fields that define what the agent sees. A hidden-only revision keeps them.
AGENT_SURFACE = ("toolchain", "deps", "deliverables")


def _environment(bundle: Path) -> dict[str, str]:
    """Every file the agent's image is built from (environment/), hashed."""
    root = Path(bundle) / "environment"
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def hidden_only_problems(before: dict, after: dict, before_bundle: Path | None = None,
                         after_bundle: Path | None = None) -> list[str]:
    """Everything the agent sees must be unchanged, or the re-grade measures nothing."""
    problems = []
    old_steps = {s.get("name"): s.get("instruction") for s in before.get("steps") or []}
    new_steps = {s.get("name"): s.get("instruction") for s in after.get("steps") or []}
    if old_steps != new_steps:
        problems.append("hidden-only revision changed a step's instruction or the step inventory; the captured "
                        "episodes no longer measure it -- use a full hardening attempt instead")
    for key in AGENT_SURFACE:
        if _digest(before.get(key)) != _digest(after.get(key)):
            problems.append(f"hidden-only revision changed `{key}`, which the agent sees")
    if before_bundle is not None and after_bundle is not None:
        old, new = _environment(before_bundle), _environment(after_bundle)
        if old != new:
            changed = sorted(set(old) ^ set(new) | {k for k in set(old) & set(new) if old[k] != new[k]})
            problems.append(f"hidden-only revision changed the agent's environment: {changed[:6]}")
    return problems


FAST_BRIEF = """\
HIDDEN-ONLY HARDENING (fast round). The step instructions, the fixture, the toolchain and the declared
deliverables are FROZEN -- byte for byte. You may change only what the agent never sees: hidden suites
(tests/suites), checks, traps, mutants, and the reference/alternative patches (only if a new published case
shows the reference itself was incomplete).

TURN BUDGET: this round has a fixed number of turns and an unsubmitted draft is lost. Plan the edits first,
stage each file in ONE stage_spec call carrying every change to it (not one call per line), call read_draft
only when you need content you have not seen, run the controls once the draft is complete, and submit_draft
well before the last fifth of your turns -- a submitted draft with a known gap beats a lost one.

You have the task's screen episodes: every captured final repository of every Fable 5.1 and GPT-6 Astra
episode. Use them:
  1. regrade_episodes -- grades every captured episode with your CURRENT draft's hidden tests (minutes) and
     returns, per model and step, how many episodes still fully pass, which checks each one fails, and how
     each failure happened (assertion / repo_crash / output = genuine; interface / suspect = NOT genuine).
  2. read_episode(model, index, path?) -- what that episode changed, or one file of its final tree.
Find what the episodes' solutions SYSTEMATICALLY get wrong on inputs the published instruction already
settles: a shape of the same datum they never handled (Unicode or full-width variants, CRLF/BOM/NBSP,
another date or number format the stated rule covers), the family of a reported case beyond the one value
they patched, an error path the user described, duplicates where first-match or last-wins differ from the
stated order, a rerun or a fault between writes, the long-tail branch of a stated rule, two stated rules
meeting on one input. Prefer what 4 or 5 of a model's 5 episodes share -- a habit generalises to fresh
episodes; one episode's typo does not.

Every new hidden case must:
  * be settled by sentences ALREADY in the instruction (cite them in the check's rule_quote);
  * pass the reference and the alternative (run_controls), and fail a mutant that writes the idiomatic wrong
    answer (an m_baseline_ mutant);
  * pass determinacy_probe: ask both families one direct question about that exact input with the answer the
    reference computes; if either family answers differently or UNDETERMINED, the instruction does not settle
    it -- drop the case, never reword the instruction;
  * fail in the re-grade by assertion or by the submission's own crash -- never by a test that cannot read
    the output, an import the instruction never named, a timeout or an environment difference.
THE HARDNESS GATE IS BINDING in this round: the session stays open until the latest regrade_episodes of the CURRENT
hidden tests shows every step fully passed by at most 1 of each model's episodes and at least 3 objective-driving traps
failing in 2 or more episodes of EACH model (creation_status lists what is missing; it waives itself only when 30
turns remain). Re-grade after every change to the tests.
TRAP BITES: regrade_episodes also returns trap_bites -- per objective-driving trap, how many episodes of each
model fail a check it owns, and no_episode_fails (traps every episode of both models gets right). Those traps
are coverage, not difficulty: aim new hidden cases first at them, at the input where the episodes' shared code
diverges from the published rule, until most traps bite and the two models fail different traps as well as shared
ones.
RESEARCH (research_traps, up to 4 questions in parallel): ask about the documented semantics of the versions this
fixture pins and the formats its data's real producers emit -- a researched fact is often exactly the shape the
episodes share a miss on. Reproduce it with sandbox_run first; it then obeys every rule above, and the trap you
add carries research {research_id, sources, finding, executed} and is typed by its ACTUAL failure mode
(library/protocol/numeric/text/temporal/absence/ordering/resource/concurrency/security/error semantics).
Do not delete or weaken any existing check. Keep the Fable-side difficulty that already bites, and add cases
aimed at what Astra's episodes miss (and Fable's, where Fable passes). The re-grade you see is IN-SAMPLE --
you chose the cases with these trees in view -- so it is reported only as provisional; the revision is then
validated and screened on FRESH episodes, and only that screen counts. Aim for a provisional full-pass rate of
0-1 of 5 per model per step so that a fresh screen lands at or under 50%.

Arena spec, Oct 12 (learning signal): aim the new cases at SEVERAL DIFFERENT habits, each its own decision, so the
re-grade shows episodes failing DIFFERENT checks with DIFFERENT partial scores -- not one case every episode fails
the same way. A set where each episode gets some decisions right and some wrong is what the model learns from.
"""

from . import learning_signal as _learning_signal
FAST_BRIEF = FAST_BRIEF + "\n" + _learning_signal.guidance()


def episode_summary(episodes: list[dict], screen_record: dict | None) -> str:
    lines = [f"CAPTURED EPISODES: {len(episodes)} valid ("
             + ", ".join(f"{m.split('/')[-1]} {sum(1 for e in episodes if e['model'] == m)}"
                         for m in sorted({e['model'] for e in episodes})) + ")."]
    rates = (screen_record or {}).get("step_pass_rates") or {}
    for model in sorted(rates):
        lines.append(f"  measured full-pass rate, {model.split('/')[-1]}: "
                     + ", ".join(f"{s} {r:.0%}" for s, r in sorted(rates[model].items())))
    return "\n".join(lines)
