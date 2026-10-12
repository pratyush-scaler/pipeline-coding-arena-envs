"""One versioned check inventory; validity, guardrails and difficulty stay separate."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

from .artifacts import atomic_json, identity
from .author.contracts import strict_json
from .config import bundle_paths
from .measurement import observation


def recount(report: dict, rubric: dict) -> tuple[dict, dict, list[str]]:
    """Count only complete recorded episodes; partial steps never earn a rate."""
    episodes, errors = {}, []
    for row in report.get("rollouts", []):
        episode = row.get("episode")
        if type(episode) is not int or episode < 1:
            errors.append("invalid episode identifier")
            continue
        episodes.setdefault(episode, []).append(row)
    counts = {step: {cid: {"passed": 0, "failed": 0, "gated_out": 0} for cid in ids}
              for step, ids in rubric.items()}
    valid = 0
    causes = Counter()
    for rows in episodes.values():
        complete = [r.get("step") for r in rows] == list(rubric)
        for row in rows:
            reward = row.get("reward")
            if (row.get("zero_class") == "void" or row.get("error")
                    or row.get("stop_reason") not in {"end_turn", "completed", "stop_sequence"}
                    or type(row.get("tool_calls")) is not int or row["tool_calls"] < 1
                    or type(reward) not in (int, float) or not math.isfinite(reward) or not 0 <= reward <= 1
                    or set(row.get("checks", {})) != set(rubric.get(row.get("step"), []))
                    or any(v not in {"passed", "failed", "gated_out"} for v in row.get("checks", {}).values())):
                complete = False
                causes[row.get("error") or row.get("stop_reason") or "missing check evidence"] += 1
        if not complete:
            continue
        valid += 1
        for row in rows:
            for cid, state in row["checks"].items():
                counts[row["step"]][cid][state] += 1
    requested = report.get("summary", {}).get("requested")
    if type(requested) is not int or requested < len(episodes):
        errors.append("requested episode count is invalid")
        requested = len(episodes)
    summary = {"requested": requested, "recorded": len(episodes), "valid": valid,
               "void_or_missing": requested - valid, "causes": dict(causes)}
    stored = report.get("summary", {})
    if stored.get("valid") != valid or stored.get("void") != requested - valid:
        errors.append("stored summary disagrees with recorded episodes")
    for step, checks in counts.items():
        declared = report.get("by_step", {}).get(step, {}).get("checks", {})
        if set(declared) != set(checks) or any(
                any(declared.get(cid, {}).get(k) != n for k, n in rec.items()) for cid, rec in checks.items()):
            errors.append(f"{step}: stored check counts disagree with complete episodes")
    return counts, summary, errors


def audit_bundle(bundle: Path) -> dict:
    sha = identity(bundle)
    definitions = strict_json((bundle / "tests/checks.json").read_text())["checks"]
    rubric = strict_json((bundle / "tests/rubric.json").read_text())
    rows = {c["id"]: {"id": c["id"], "kind": c["kind"], "weight": c.get("weight", 0),
                      "steps": [s for s, ids in rubric.items() if c["id"] in ids],
                      "models": {}} for c in definitions}
    errors, campaigns = [], []
    if len(rows) != len(definitions):
        errors.append("duplicate check definitions")
    if set(rows) != {cid for ids in rubric.values() for cid in ids}:
        errors.append("rubric and check definitions disagree")
    for path in sorted((bundle / "verification").glob("calibration-*.json")):
        report = strict_json(path.read_text())
        counts, summary, issues = recount(report, rubric)
        version_matches = report.get("grader_fingerprint") == sha
        if not version_matches:
            issues.append("campaign measured a different task version")
        model = report.get("model", "unknown")
        campaigns.append({"model": model, "report": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "protocol": report.get("protocol"), "effort": report.get("effort"),
            "summary": summary, "errors": issues,
            "evidence_status": "unverified: descriptive recount, not native release evidence"})
        for step, step_counts in counts.items():
            for cid, rec in step_counts.items():
                if cid not in rows:
                    continue
                measured = observation(rec)
                state = ("invalid_evidence" if issues else "void" if not summary["valid"]
                         else "unreachable" if not measured["attempted"] else "unreviewed")
                model_rows = rows[cid]["models"].setdefault(model, {})
                if step in model_rows:
                    errors.append(f"duplicate model/step evidence {model}/{step}/{cid}")
                model_rows[step] = {**measured, "validity": state,
                    "band": measured["band"] if state == "unreviewed" else None,
                    "void_episodes": summary["void_or_missing"]}
    for row in rows.values():
        states = {r["validity"] for steps in row["models"].values() for r in steps.values()}
        row["validity"] = next((s for s in ("invalid_evidence", "void", "unreachable", "unreviewed") if s in states), "unmeasured")
        row["action"] = {"invalid_evidence": "repair or recover matching execution evidence",
            "void": "repair execution and rerun; no capability inference",
            "unreachable": "repair prerequisites and rerun",
            "unreviewed": "adjudicate failures, then select or redesign substantive decisions",
            "unmeasured": "validate and measure"}[row["validity"]]
    return {"bundle": str(bundle), "artifact_sha256": sha, "errors": errors,
            "checks": list(rows.values()), "campaigns": campaigns, "release_ready": False}


def audit(root: Path) -> dict:
    bundles, errors = [], []
    found = set(bundle_paths(root))
    found.update(p.parent for pattern in ("task.toml", "*/task.toml", "*/*/task.toml") for p in root.glob(pattern))
    for bundle in sorted(found):
        try:
            bundles.append(audit_bundle(bundle))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append({"bundle": str(bundle), "error": str(exc)})
    rows = [r for b in bundles for r in b["checks"]]
    return {"schema": "arena-pipeline/check-inventory/1", "bundles": bundles, "errors": errors,
        "summary": {"tasks": len(bundles), "unique_checks": len(rows),
                    "by_kind": dict(Counter(r["kind"] for r in rows)),
                    "by_validity": dict(Counter(r["validity"] for r in rows))},
        "policy": {"floor": ">=90%", "core": "30-70%", "stretch": "<30%", "transition": ">70% and <90%",
                   "denominator": "passed + failed in complete non-void episodes, per model and step",
                   "inventory_key": "task artifact hash and check ID; shared guardrails counted once",
                   "limitation": "Raw recount is not semantic adjudication. No automatic label of legitimate model failure; no pooled model bands."}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("-o", "--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = audit(args.root)
    atomic_json(args.out, result)
    print(json.dumps(result["summary"], indent=2))
    return 1 if result["errors"] or any(b["errors"] for b in result["bundles"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
