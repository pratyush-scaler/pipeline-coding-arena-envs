"""Why the models failed: the failure-cause columns Arena asked for on the cover sheet.

Arena, after the Oct 12 review: "In the cover sheet, can you add columns for explaining why the
models failed? This will help both of us to keep a track on diversity around failure causes."

Derived, never typed: the genuine-failure audit (verification/genuine.json) names, per valid
episode, every check it failed, the failure kind (assertion, crash, contract ...) and the
instruction sentence(s) the check enforces; the spec links each check to its primary trap, whose
type and `beatable_by` say which reading the task rewards. The calibration's learning signal
(pipeline.learning_signal) adds the grader shape.

    report(package) -> dict      write(package) -> FAILURES.md + verification/failure-causes.json
    cover_row(doc)  -> the cover-sheet columns for one task
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA = "arena-coding/failure-causes/1"
MARKDOWN = "FAILURES.md"
RECEIPT = "verification/failure-causes.json"


def _read(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def report(package: Path, signal: dict | None = None) -> dict:
    package = Path(package)
    spec = _read(package / "authoring/spec.json") or {}
    audit = _read(package / "verification/genuine.json") or {}
    checks = {c.get("id"): c for c in spec.get("checks") or [] if isinstance(c, dict)}
    traps = {t.get("id"): t for t in spec.get("traps") or [] if isinstance(t, dict)}
    models: dict[str, dict] = defaultdict(lambda: {"episodes": 0, "failed_episodes": 0, "kinds": Counter()})
    per_check: dict[str, dict] = {}
    for ep in audit.get("episodes") or []:
        if not ep.get("valid") or ep.get("verdict") == "void":
            continue
        m = models[ep.get("model") or "?"]
        m["episodes"] += 1
        fails = ep.get("failures") or []
        if fails:
            m["failed_episodes"] += 1
        for f in fails:
            cid = f.get("check")
            m["kinds"][f.get("kind") or "unknown"] += 1
            row = per_check.setdefault(cid, {"check": cid, "step": f.get("step"), "by_model": Counter(), "kinds": Counter(),
                                             "published": f.get("published") or []})
            row["by_model"][ep.get("model") or "?"] += 1
            row["kinds"][f.get("kind") or "unknown"] += 1
    rows = []
    for cid, row in per_check.items():
        c = checks.get(cid) or {}
        trap_id = c.get("trap")[0] if isinstance(c.get("trap"), list) and c.get("trap") else c.get("trap")
        t = traps.get(trap_id) or {}
        rows.append({"check": cid, "step": row["step"], "kind_of_check": c.get("kind"), "weight": c.get("weight"),
                     "failed_runs": dict(row["by_model"]), "failure_kinds": dict(row["kinds"]),
                     "trap": trap_id, "trap_type": t.get("type"),
                     "what_the_task_rewards": t.get("beatable_by"), "why_it_misleads": t.get("grounding"),
                     "instruction_sentences": row["published"][:3]})
    rows.sort(key=lambda r: (-sum(r["failed_runs"].values()), -(r["weight"] or 0)))
    type_counts = Counter()
    for r in rows:
        if r["trap_type"]:
            type_counts[r["trap_type"]] += sum(r["failed_runs"].values())
    return {"schema": SCHEMA, "task": package.name,
            "models": {k: {"valid_episodes": v["episodes"], "failed_episodes": v["failed_episodes"],
                           "failure_kinds": dict(v["kinds"])} for k, v in models.items()},
            "checks": rows, "failure_trap_types": dict(type_counts.most_common()),
            "signal": signal, "source": "verification/genuine.json + authoring/spec.json traps"}


def cover_row(doc: dict) -> dict:
    """The cover-sheet columns: decisive trap type(s), the leading failure causes, how they failed."""
    rows = doc.get("checks") or []
    lead = rows[:3]

    def cause(r):
        runs = ", ".join(f"{m.split('/')[-1]} {n}" for m, n in r["failed_runs"].items())
        why = (r.get("what_the_task_rewards") or r.get("why_it_misleads") or "").strip().rstrip(".")
        return f"{r['check']} [{r.get('trap_type') or 'untyped'}; failed {runs}]" + (f": {why[:220]}" if why else "")

    kinds = Counter()
    for m in (doc.get("models") or {}).values():
        kinds.update(m.get("failure_kinds") or {})
    signal = doc.get("signal") or {}
    return {"decisive_trap_types": list((doc.get("failure_trap_types") or {}).keys())[:3],
            "why_models_failed": [cause(r) for r in lead],
            "failure_kinds": dict(kinds.most_common()),
            "independent_hard_decisions": signal.get("independent_hard"),
            "free_weight_share": signal.get("free_weight_share"),
            "reward_spread": {m.split("/")[-1]: (s or {}).get("stdev") for m, s in (signal.get("spread") or {}).items()}}


def markdown(doc: dict) -> str:
    row = cover_row(doc)
    lines = ["# Why the models failed", "",
             "Derived from the genuine-failure audit of the shipped calibration and the spec's trap records; "
             "nothing here is typed by hand (`pipeline/failure_report.py`).", ""]
    if row["decisive_trap_types"]:
        lines += [f"- **Decisive trap type(s):** {', '.join(row['decisive_trap_types'])}"]
    if row["independent_hard_decisions"] is not None:
        lines += [f"- **Independent hard decisions:** {row['independent_hard_decisions']}; "
                  f"**free weight:** {row['free_weight_share']}; **reward spread (stdev):** {row['reward_spread']}"]
    lines += [f"- **Failure kinds:** {row['failure_kinds']}", "", "| model | valid runs | runs with a failure |",
              "|---|---:|---:|"]
    for m, v in (doc.get("models") or {}).items():
        lines.append(f"| {m.split('/')[-1]} | {v['valid_episodes']} | {v['failed_episodes']} |")
    lines += ["", "| check | trap type | failed runs | what the task rewards | instruction sentence |",
              "|---|---|---|---|---|"]
    for r in doc.get("checks") or []:
        runs = ", ".join(f"{m.split('/')[-1]} {n}" for m, n in r["failed_runs"].items())
        esc = lambda s: str(s or "").replace("|", "\\|").replace("\n", " ")[:260]
        lines.append(f"| `{r['check']}` | {r.get('trap_type') or ''} | {runs} | {esc(r.get('what_the_task_rewards'))} | "
                     f"{esc((r.get('instruction_sentences') or [''])[0])} |")
    return "\n".join(lines) + "\n"


def write(package: Path, signal: dict | None = None, source: Path | None = None) -> dict:
    """Write into `package`, reading the audit, spec and release receipt from `source` (the released
    version) when the delivered copy does not carry them."""
    package = Path(package)
    source = Path(source) if source else package
    if signal is None:
        rel = _read(source / "verification/shipfix-release.json") or {}
        signal = (((rel.get("steps") or {}).get("hardness") or {}).get("result") or {}).get("signal")
    doc = report(source if (source / "verification/genuine.json").is_file() else package, signal)
    doc["task"] = package.name
    (package / "verification").mkdir(exist_ok=True)
    (package / RECEIPT).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (package / MARKDOWN).write_text(markdown(doc), encoding="utf-8")
    return doc


if __name__ == "__main__":
    import sys
    print(json.dumps(cover_row(write(Path(sys.argv[1]))), indent=1, ensure_ascii=False))
