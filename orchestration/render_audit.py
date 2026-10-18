"""Render deliverable-zips-coding-v2/SPEC-AUDIT.md and INDEX.md from spec-audit.json + index.json.

Batch criteria follow Arena's RL Environments Spec (Updated 9_28), computed over the tasks in
this folder (zipped + held). A criterion that cannot be computed here is stated as such.
"""
import json, time
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "ship-zips"
audit = json.loads((OUT / "spec-audit.json").read_text()) if (OUT / "spec-audit.json").exists() else {}
index = json.loads((OUT / "index.json").read_text()) if (OUT / "index.json").exists() else {"tasks": {}, "held": {}}
tasks, held = index.get("tasks", {}), index.get("held", {})
names = sorted(set(tasks) | set(held) | set(audit))

COLS = ["harbor_format", "harbor_tasks_check", "session_id", "offline_isolated", "gate_chain", "no_answer_leaks",
        "grader_sanity", "grader_deterministic", "no_llm_judge", "hack_probes", "failures_genuine", "review_clean",
        "solvability", "difficulty", "reward_hacking", "calibration_settings", "calibration_runs_per_spec", "faithfulness"]
MARK = {"pass": "ok", "fail": "FAIL", "unknown": "?"}


def pct(a, b):
    return f"{a}/{b} ({100 * a / b:.0f}%)" if b else "0/0"


n = len(names)
def ok(name, *keys):
    c = (audit.get(name) or {}).get("checks") or {}
    return all((c.get(k) or {}).get("status") == "pass" for k in keys)

quality = sum(ok(x, "grader_sanity", "solvability", "difficulty") for x in names)
reviewable = sum(ok(x, "harbor_tasks_check") and x in tasks for x in names)
unresolved = [(x, f) for x in names for f in (audit.get(x) or {}).get("flags") or [] if f.get("disposition") == "UNRESOLVED"]
levels = {}
for x in names:
    lv = (tasks.get(x) or held.get(x) or {}).get("level") or ((audit.get(x) or {}).get("checks", {}).get("faithfulness", {}).get("evidence", "").split()[-1:] or ["?"])[0]
    levels[x] = lv
f1 = sum(levels[x] in ("F1", "F2", "F3") for x in names)
f2 = sum(levels[x] in ("F2", "F3") for x in names)
multi = sum(len((audit.get(x) or {}).get("steps") or []) > 1 for x in names)

lines = ["# Spec audit -- Arena RL Environments Spec (Updated 9_28)", "",
         f"Generated {time.strftime('%Y-%m-%d %H:%M')} over {n} task(s): {len(tasks)} zipped, {len(held)} held by delivery.", "",
         "Calibration plan (user, 2026-10-05): Fable 5.1 (high, Claude Code) 10 episodes; GPT 6 Astra (high, Codex) = the 5-episode "
         "screen of record. The spec asks for 10 runs per model, so `calibration_runs_per_spec` fails for Astra on every task by design.", "",
         "## Batch acceptance criteria", "",
         "| Criterion | Spec threshold | This folder | Status |", "|---|---|---|---|",
         f"| Reviewable | 100%: harbor tasks-check pass, calibration runs + provenance included | tasks-check receipts: {pct(reviewable, n)} | {'ok' if reviewable == n and n else 'FAIL'} |",
         f"| Task quality | >=90% meet grader sanity, solvability, general difficulty | {pct(quality, n)} | {'ok' if n and quality / n >= 0.9 else 'FAIL'} |",
         f"| Reward hacking | 0 confirmed or unresolved | {len(unresolved)} unresolved | {'ok' if not unresolved else 'FAIL'} |",
         f"| Faithfulness mix | no F-1; >=50% F1+; >=15% F2+ | F1+ {pct(f1, n)}, F2+ {pct(f2, n)}; levels {sorted(set(levels.values()))} | {'ok' if n and f1 / n >= 0.5 and f2 / n >= 0.15 else 'FAIL'} |",
         "| Trace accounting | 100% of shared traces dispositioned | needs the batch's shared session list (computed by `arena deliverable`, not here) | not computed |",
         f"| Multi-turn | >=10% of environments include a multi-turn task | {pct(multi, n)} | {'ok' if n and multi / n >= 0.1 else 'FAIL'} |",
         "", "## Per-task checks", "",
         "| Task | " + " | ".join(c.replace('_', ' ') for c in COLS) + " | Fable 10 | Astra 5 |",
         "|" + "---|" * (len(COLS) + 3)]
for x in names:
    a = audit.get(x) or {}
    c = a.get("checks") or {}
    cal = a.get("calibration") or {}
    fr, ar = cal.get("fable") or {}, cal.get("astra") or {}
    lines.append(f"| {x} | " + " | ".join(MARK.get((c.get(k) or {}).get("status"), "-") for k in COLS) +
                 f" | {fr.get('pass')}/{fr.get('scored')} p@4 {fr.get('pass_at_4')} r {fr.get('mean_reward')} | {ar.get('pass')}/{ar.get('scored')} p@4 {ar.get('pass_at_4')} r {ar.get('mean_reward')} |")
lines += ["", "## Failures and their evidence", ""]
for x in names:
    a = audit.get(x) or {}
    if a.get("error"):
        lines.append(f"- **{x}**: audit error -- {a['error']}")
    for k, cc in (a.get("checks") or {}).items():
        if cc["status"] != "pass" and k != "calibration_runs_per_spec":
            lines.append(f"- **{x}** `{k}`: {cc['evidence']}")
    if x in held:
        lines.append(f"- **{x}** held by delivery: {held[x]['why']}")
lines += ["", "## Reward-hacking flags", ""]
flags = [(x, f) for x in names for f in (audit.get(x) or {}).get("flags") or []]
lines += [f"- {x} {f['model']} {f['trial']} ({f['kind']}): {f['disposition']}" for x, f in flags] or ["- none"]
(OUT / "SPEC-AUDIT.md").write_text("\n".join(lines) + "\n")

idx = ["# Coding task deliverables (v2)", "",
       "Each zip holds one Harbor task (schema 1.4) as delivered: environment, steps, grader (tests/), reference solution, "
       "provenance with the source Arena session id, verification receipts, runs/ (every calibration run with per-rubric "
       "rewards, plus the reference trajectory) and REWARD-HACKING.md. A multi-step task also gets one zip per step. "
       "See SPEC-AUDIT.md for the check-by-check audit against the spec.", "",
       "| Task | Zip | Level | Steps | Fable (pass@1) | Astra (pass@1) | Open audit items |", "|---|---|---|---|---|---|---|"]
for x, t in sorted(tasks.items()):
    f, a = t.get("fable_10") or {}, t.get("astra_10") or t.get("astra_5") or {}
    open_items = [k for k in t.get("audit_fails") or [] if k != "calibration_runs_per_spec"]
    idx.append(f"| {x} | {t['zip']}{' + ' + str(len(t['step_zips'])) + ' step zips' if t.get('step_zips') else ''} | {t['level']} | "
               f"{len(t.get('steps') or [])} | {f.get('pass')}/{f.get('scored')} ({f.get('pass_at_1')}) | "
               f"{a.get('pass')}/{a.get('scored')} ({a.get('pass_at_1')}) | {', '.join(open_items) or '-'} |")
if held:
    idx += ["", "## Held by delivery (not zipped)", ""] + [f"- {x}: {h['why']}" for x, h in sorted(held.items())]
(OUT / "INDEX.md").write_text("\n".join(idx) + "\n")
print("rendered", len(names))
