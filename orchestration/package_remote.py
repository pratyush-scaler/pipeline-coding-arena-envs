"""Package ready tasks ON their workspace, where bundles and campaigns are authoritative.

Stages each bundle under its lineage name, runs the pipeline's `deliver` (allowlist,
a_f<level>_<slug> rename, delivery receipt, privacy refusal; the level is measured with the
native campaigns present), then adds what the spec's "Runs and results" asks for:

    runs/claude-fable-5-1/<campaign>/   the 10-episode Fable 5.1 campaign
    runs/gpt-6-astra/<campaign>/        the Astra campaign(s) of the 5-episode screen of record
    runs/reference/<campaign>/          the native oracle control (reference solution through the grader)
    runs/index.json                     model, trial id, reward, per-rubric rewards per trial
    REWARD-HACKING.md                   what was tested, found and fixed

and re-runs the delivery privacy scan over the finished package, runs included; a finding
removes the task from the output and is reported.

usage: python package_remote.py <tasks.json> <outdir>
"""
import json, shutil, sys, time
from pathlib import Path

from pipeline import deliver as D
from pipeline.deliverable import open_privacy, privacy_summary

SKIP = shutil.ignore_patterns("reviews", "local-workers", "hardened", "__pycache__", "*.pyc", ".build.lock",
                              "controls-work", "control-cache", "design-sessions", "design-attempts")


def trials(campaign: Path):
    rows = []
    for t in sorted((campaign / "jobs/run").glob("package__*")):
        r = json.loads((t / "result.json").read_text()) if (t / "result.json").is_file() else {}
        reward = ((r.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        steps = {}
        for d in sorted((t / "steps").glob("*/verifier/reward-details.json")):
            det = json.loads(d.read_text()).get("reward") or {}
            steps[d.parent.parent.name] = {"score": det.get("score"),
                                           "criteria": {c.get("name"): c.get("value") for c in det.get("criteria") or []}}
        rows.append({"trial_id": t.name, "reward": reward, "exception": bool(r.get("exception_info")), "per_step_rubric": steps})
    return rows


def note(task, bundle: Path, audit: dict) -> str:
    v = bundle / "verification"
    ctl = json.loads((v / "controls.json").read_text()) if (v / "controls.json").is_file() else {}
    pr = json.loads((v / "probes.json").read_text()) if (v / "probes.json").is_file() else {}
    res = ctl.get("results") or {}
    tested = sorted(k for k in res if k not in ("determinism",))
    probes = sorted((pr.get("results") or {}).keys())
    lines = [f"# Reward hacking: {task['name']}", "",
             "## What was tested",
             f"- Executed controls ({len(tested)}): {', '.join(tested)}. "
             "Each mutant fails exactly the checks its trap declares; the untouched workspace and destructive/empty "
             "submissions score 0; the reference and an independently written fix score 1.0 "
             f"(determinism: {json.dumps(res.get('determinism'))}).",
             f"- Hack probes ({len(probes)}): {', '.join(probes)} -- ok: {pr.get('ok')}.",
             "- The verifier is built from tests/ alone; /tests is root-owned and read-only and every test runs as the "
             "unprivileged grader user; a check counts only tests its runner reports as completed (exit status alone is "
             "never evidence).",
             f"- Every calibration trajectory was scanned for test access, fake outputs, files dumped into graded "
             f"directories, reused caches, symlink tricks and parser/exit-code exploits: "
             f"{audit.get('scanned')}.", "", "## What was found"]
    flags = audit.get("flags") or []
    if not flags:
        lines.append("- No reward-hacking flag in any calibration run.")
    for f in flags:
        lines.append(f"- {f['model']} {f['trial']}: {f['kind']} -- {f['disposition']}")
    lines += ["", "## What was fixed"]
    fixed = audit.get("fixed") or []
    lines += [f"- {x}" for x in fixed] or ["- Nothing needed fixing for reward hacking in this version."]
    # Critic gaps the judges demoted ship listed, never hidden (user, 2026-10-09: "Block only realistic gaps").
    critic = json.loads((v / "coverage-critic.json").read_text()) if (v / "coverage-critic.json").is_file() else {}
    advisory = [g for g in critic.get("gaps") or [] if isinstance(g, dict) and g.get("blocking", True) is False]
    lines += ["", "## Known coverage gaps (advisory)",
              "- A coverage critic wrote wrong implementations of published clauses on purpose and the shipped grader "
              "executed each one; every gap that is realistic and clearly settled by the instruction was closed and "
              "re-verified on this version" + (" (" + str(critic.get("mode")) + " run)" if critic.get("mode") else "") + "."]
    lines += [f"- Not enforced -- {(g.get('judgement') or {}).get('why')}: a wrong implementation scores 1.0 while "
              f"violating \"{str(g.get('clause'))[:200]}\": {str(g.get('violation'))[:300]}" for g in advisory] or [
              "- None: no executed gap was judged contrived or unsettled by the instruction."]
    return "\n".join(lines) + "\n"


# F0 ships (user, 2026-10-05 ~22:45 IST: "We can ship F0 task so there's no issue with that"; spec
# 9_28 admits F0 "Inspired" with evidence and forbids only F-1). The pipeline's delivery still refuses
# F0 (policy.SPEC_F0_ALLOWED, written for 9_21), so the plan is adjusted here, in this process only:
# F0 rows deliver to F0-inspired/, and each carries FAITHFULNESS.md with the trace evidence.
from pipeline import policy as _policy
_policy.DELIVERY_DIRS = {**_policy.DELIVERY_DIRS, "F0": "F0-inspired"}
_plan = D.plan


def _plan_with_f0(root):
    doc = _plan(root)
    for r in doc["rows"]:
        if r.get("level") == "F0":
            r.update(rejected=False, reason=None, destination=f"F0-inspired/{r['short_name']}")
    return doc


D.plan = _plan_with_f0


def faithfulness_note(bundle: Path, level: str) -> str:
    spec = json.loads((bundle / "authoring/spec.json").read_text())
    ledger = json.loads((bundle / "provenance/ledger.json").read_text())
    rec = ledger.get("faithfulness_level_record") or {}
    src = spec.get("source") or {}
    lines = [f"# Faithfulness: {level} ({rec.get('level_source') or 'Inspired'})", "",
             f"Source Arena session: {src.get('session_id')} (seed {src.get('seed_index')}).", "",
             "Spec 9_28, F0 Inspired: a concrete user goal and the capability needed to complete it are recoverable "
             "from the trace; the task uses a synthetic scenario and verifier exercising that same goal and capability.", "",
             "## The user goal taken from the session", "", str(spec.get("description") or "")[:1500], "",
             "## Evidence of what was taken from the trace", ""]
    for t in spec.get("traps") or []:
        if t.get("grounding"):
            lines.append(f"- trap {t.get('id')}: {t['grounding']}")
    for lp in (spec.get("quality") or {}).get("lever_plan") or []:
        if lp.get("grounding"):
            lines.append(f"- decision: {lp['grounding']}")
    lines += ["", "## Why it is not higher (measured)", ""] + [f"- {w}" for w in rec.get("why_not_higher") or []]
    return "\n".join(lines) + "\n"


def main():
    tasks = json.loads(Path(sys.argv[1]).read_text())
    outdir = Path(sys.argv[2])
    work, out = outdir / "work", outdir / "out"
    work.mkdir(parents=True)
    by_name = {}
    import hashlib
    from pipeline import privacy as _privacy
    redactions = {}
    for t in tasks:
        shutil.copytree(t["bundle"], work / t["name"], ignore=SKIP, symlinks=True)
        by_name[t["name"]] = t
        if t.get("redact_oracle"):
            # User-approved (2026-10-05, "lets redact and zip these tasks"): the independent oracle's
            # authoring transcript/patch quotes email-shaped text (public package.json maintainers;
            # synthetic test addresses). Redacted in this STAGED copy only, with the pipeline's own
            # excerpt redaction, before delivery's privacy scan; graded/environment/solution files are
            # never touched. Each file's original and redacted sha256 is recorded in REDACTIONS.md.
            staged = work / t["name"]
            for fnd in _privacy.scan(staged):
                rel = str(fnd.get("file", ""))
                if not rel.startswith("authoring/oracles/") or fnd.get("kind") == "unscannable":
                    continue
                fp = staged / rel
                raw = fp.read_bytes()
                text = raw.decode("utf-8")
                new_text = _privacy.redact_excerpt(text)
                if new_text != text:
                    fp.write_text(new_text, encoding="utf-8")
                    redactions.setdefault(t["name"], []).append(
                        {"file": rel, "kind": fnd.get("kind"), "original_sha256": hashlib.sha256(raw).hexdigest(),
                         "redacted_sha256": hashlib.sha256(new_text.encode("utf-8")).hexdigest()})
    receipt = D.deliver(work, out)
    report = {"delivered": [], "held": []}
    for row in receipt.get("held", []):
        report["held"].append({"name": Path(row["source"]).name, "why": str(row.get("held_because"))[:600], "level": row.get("level")})
    for row in receipt.get("delivered", []):
        name = Path(row["source"]).name
        t = by_name[name]
        dest = out / row["destination"]
        runs = dest / "runs"
        index = {"schema": "arena-coding-v2/runs/1", "campaigns": [],
                 "excluded": "agent/sessions/ in every trial: the harness CLI's internal state directory (settings, policy "
                             "limits, raw session store); agent/trajectory.json and the harness transcript hold the full trajectory"}
        for label, paths in (("claude-fable-5-1", [t.get("fable_campaign")]), ("gpt-6-astra", t.get("astra_campaigns") or []),
                             ("reference", [t.get("oracle_campaign")])):
            for p in paths:
                if not p or not Path(p).is_dir():
                    if p: index["campaigns"].append({"label": label, "source": p, "missing": True})
                    continue
                target = runs / label / Path(p).name
                # agent/sessions is the CLI's own state directory (settings, policy limits, backups, debug
                # logs, the raw session store), not the trajectory: agent/trajectory.json and the harness
                # transcript carry every prompt, turn and tool call. It held Claude Code's commit-attribution
                # address and must not ship (privacy scan, 2026-10-05).
                shutil.copytree(p, target, symlinks=False, ignore=shutil.ignore_patterns("*.sock", "sessions"))
                index["campaigns"].append({"label": label, "source": p, "path": str(target.relative_to(dest)), "trials": trials(Path(p))})
        runs.mkdir(parents=True, exist_ok=True)
        # Calibration transcripts quote whatever the agents read or wrote: test URLs with userinfo on
        # reserved domains, public mailing-list and spec-editor addresses from fetched pages. Outside the
        # fixture the delivery privacy scan cannot resolve a finding, so runs/ text is passed through the
        # pipeline's own excerpt redaction (privacy.redact_excerpt). Task files are never touched; every
        # redacted file is listed. Rewards and per-rubric scores are unaffected.
        from pipeline import privacy
        redacted = []
        for finding in privacy.scan(dest):
            rel = str(finding.get("file", ""))
            if not rel.startswith("runs/") or finding.get("kind") == "unscannable":
                continue
            fp = dest / rel
            try:
                text = fp.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            new_text = privacy.redact_excerpt(text)
            if new_text != text:
                fp.write_text(new_text)
                redacted.append({"file": rel, "kind": finding.get("kind")})
        index["redacted"] = sorted({(r["file"], r["kind"]) for r in redacted})
        (runs / "index.json").write_text(json.dumps(index, indent=1))
        (dest / "REWARD-HACKING.md").write_text(note(t, Path(t["bundle"]), t.get("audit") or {}))
        # Spec Appendix A: a multi-turn package carries, per turn, the user prompt, the state passed
        # between turns, the expected outcome and the rollout results (TURNS.md + verification/turns.json,
        # generated from the package itself). A package missing any of them is held.
        from pipeline import turns as _turns
        if len([p for p in (dest / "steps").iterdir() if p.is_dir()]) >= 2:
            _turns.write(dest)
            turn_problems = _turns.problems(dest)
            if turn_problems:
                shutil.rmtree(dest)
                report["held"].append({"name": name, "level": row.get("level"),
                                       "why": "multi-turn contract: " + "; ".join(turn_problems)[:600]})
                continue
        # Arena (Oct 12): the cover sheet explains why the models failed. FAILURES.md +
        # verification/failure-causes.json, derived from the shipped calibration's genuine audit and the traps.
        from pipeline import failure_report as _failures
        try:
            _failures.write(dest, source=Path(t["bundle"]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report.setdefault("warnings", []).append({"name": name, "failure_report": f"{type(exc).__name__}: {exc}"[:300]})
        if row.get("level") == "F0":
            (dest / "FAITHFULNESS.md").write_text(faithfulness_note(Path(t["bundle"]), "F0"))
        if redactions.get(name):
            rl = ["# Redactions", "",
                  "The independent oracle (a different-vendor model that solved this task during authoring to cross-check "
                  "the reference) quoted email-shaped text in its transcript/patch: public open-source maintainer addresses "
                  "from a dependency's package.json, synthetic test addresses, or code the scanner reads as an address. "
                  "Delivery's privacy scan cannot clear a finding outside the fixture, so with the operator's approval "
                  "(2026-10-05) these files were passed through the pipeline's excerpt redaction (privacy.redact_excerpt) "
                  "in the delivered copy only. No environment, instruction, test, grader or solution file was changed; "
                  "difficulty and validity were measured on the unredacted task and are unaffected. The oracle's own "
                  "receipt hashes the original files; their original sha256 values are listed here.", "",
                  "| File | Kind | Original sha256 | Redacted sha256 |", "|---|---|---|---|"]
            rl += [f"| {r['file']} | {r['kind']} | {r['original_sha256']} | {r['redacted_sha256']} |" for r in redactions[name]]
            (dest / "REDACTIONS.md").write_text("\n".join(rl) + "\n")
        findings = open_privacy(dest)
        # A binary file the scan cannot read is closed only by a visual review of those exact bytes
        # (the pipeline's rule for fixture images), recorded by sha256 with what was seen.
        import hashlib
        reviewed = t.get("reviewed_unscannable") or {}
        closed = []
        for fnd in list(findings):
            fp = dest / fnd["file"]
            if fnd["kind"] == "unscannable" and fp.is_file() and hashlib.sha256(fp.read_bytes()).hexdigest() in reviewed:
                closed.append({"file": fnd["file"], "review": reviewed[hashlib.sha256(fp.read_bytes()).hexdigest()]})
                findings.remove(fnd)
        # A run's captured output can hold thousands of generated binaries (c-019: 2,023 icon PNGs)
        # that no scan can read and no visual review can cover. Binary files under a run's
        # artifacts/ that are neither reviewed nor readable are omitted and listed; the grade of
        # every trial stays in its verifier/reward-details.json, and the trajectory is untouched.
        omitted = []
        for fnd in list(findings):
            rel = fnd["file"]
            if fnd["kind"] == "unscannable" and rel.startswith("runs/") and "/artifacts/" in rel:
                fp = dest / rel
                if fp.is_file():
                    omitted.append({"file": rel, "bytes": fp.stat().st_size,
                                    "sha256": hashlib.sha256(fp.read_bytes()).hexdigest()})
                    fp.unlink()
                findings.remove(fnd)
        # Any other finding on an omitted file goes with its bytes: the plain byte scan reads compressed
        # PNG pixel data as an "email" (c-019, 2026-10-08), and those bytes no longer ship.
        gone = {o["file"] for o in omitted}
        findings = [f for f in findings if f["file"] not in gone]
        if closed or omitted:
            idx = json.loads((runs / "index.json").read_text())
            if closed:
                idx["visually_reviewed"] = closed
            if omitted:
                idx["omitted_unscannable_artifacts"] = {"count": len(omitted), "why": "binary agent outputs the privacy scan cannot read; omitted rather than shipped unchecked", "files": omitted}
            (runs / "index.json").write_text(json.dumps(idx, indent=1))
        if findings:
            shutil.rmtree(dest)
            report["held"].append({"name": name, "why": "privacy (with runs): " + privacy_summary(findings), "level": row.get("level")})
            continue
        # Ship-time sweep (user, 2026-10-07: "if these kind of issues are found while shipping a task then
        # lets fix them and then ship"). Swept on the SOURCE bundle -- its extraction record, certificate and
        # coverage-critic receipt -- against the runs just exported. Open findings hold the task with each
        # finding's fix route; it is fixed (`shipcheck` routes), re-measured, and packaged again.
        # image=True: every import resolves and every interpreter is clean in the agent image;
        # repro=True: both images rebuild from the package alone (--no-cache) and the reference
        # scores 1.0, the fixture 0 (user, 2026-10-07: "Arena team should be able to reproduce
        # the env without any issues").
        from pipeline import shipcheck
        sweep = shipcheck.sweep_for_shipping(Path(t["bundle"]), runs=runs)
        (dest / shipcheck.RECEIPT).parent.mkdir(parents=True, exist_ok=True)
        (dest / shipcheck.RECEIPT).write_text(json.dumps(sweep, indent=1, default=str))
        if not sweep["ok"]:
            shutil.rmtree(dest)
            report["held"].append({"name": name, "level": row.get("level"), "shipcheck": sweep,
                                   "why": f"shipcheck: {sweep['open']} open finding(s), re-measure {sweep['measure']}: "
                                          + "; ".join(f"{f['check']} {f['subject']} [{f['route']}]"
                                                      for f in sweep["findings"] if not f.get("waived"))[:600]})
            continue
        report["delivered"].append({"name": name, "short_name": row["short_name"], "level": row["level"],
                                    "destination": row["destination"], "accepted": row.get("accepted"),
                                    "acceptance_problems": (row.get("acceptance_problems") or [])[:4],
                                    "steps": sorted(p.name for p in (dest / "steps").iterdir() if p.is_dir())})
    (outdir / "package-report.json").write_text(json.dumps(report, indent=1))
    print("PKG " + json.dumps({"delivered": [d["name"] for d in report["delivered"]], "held": report["held"]}))


main()
