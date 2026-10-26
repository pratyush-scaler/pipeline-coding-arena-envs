"""The bash RLE wave: classified seeds richest in shell failures, stamped for the bash RLE track.

User, 2026-10-26: "we need to add a branch to the pipeline to create bash failure
tasks which are rich in bash failure outcomes".

The first wave's seeds were picked by hand and each row carried an operator brief with
the trace's shell failures pasted in. This step reads every surviving seed's trace,
profiles the failed shell calls inside its span (pipeline/dump/failures.py), keeps the
seeds with at least `policy.BASH_RLE_MIN_FAILED_SHELL_CALLS`, and writes a frozen wave
whose rows carry `track: "bash_rle"` (the designer then leads with
pipeline/author/bash_rle.MANDATE) and `track_priority` (orchestrate orders by it inside a
tier). Build it like any wave:

    ./arena bash-rle-wave -o pipeline-artifacts/bash-rle-wave.json
    ./arena build -c pipeline-artifacts/bash-rle-wave.json -o envs-bash-rle ...

A failure the trace's agent hit is a lead, not a measurement: what makes a task hard is
still measured by the screen and the release calibration.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from .. import policy
from ..author.bash_rle import TRACK
from ..config import ARTIFACTS, sessions_root
from ..dump import failures as failures_mod
from ..dump import recover as rec
from ..dump import signals as sig

#: Classes that are not a shell decision going wrong: a search that found nothing, an unclassified exit.
NOT_DECISIONS = ("no_match", "other")


def profile(trace: dict, outcomes: dict | None, span) -> dict:
    """What the seed's span offers a bash RLE designer, from its failed tool calls."""
    result = failures_mod.digest(trace, outcomes, span)
    rows = [r for r in result["failures"] if r["in_span"] and r["tool"] == "bash"]
    decisions = [r for r in rows if r["class"] not in NOT_DECISIONS]
    classes = Counter(r["class"] for r in decisions)
    return {
        "failed_shell_calls": len(rows),
        "decision_failures": len(decisions),
        "prerequisite_failures": sum(r["prerequisite"] for r in decisions),
        "arena_bash_reliability_failures": result["summary"]["arena_bash_reliability_in_span"].get("failure", 0),
        "classes": dict(classes.most_common()),
        "repeated": sum(1 for r in decisions if r["attempt"] > 1),
        "pushed_back": sum(1 for r in decisions if r["rejected_by_user"]
                           or (r["user_next"] and r["user_next"]["signals"])),
        "missing_programs": result["summary"]["missing_programs"],
    }


def score(p: dict) -> float:
    """Prerequisite failures count half (Arena's metric leaves them out; they still make setup), Arena-flagged
    failures and distinct classes count extra: the wave wants several kinds of shell decision per seed."""
    plain = p["decision_failures"] - p["prerequisite_failures"]
    return round(plain + 0.5 * p["prerequisite_failures"] + 2.0 * p["arena_bash_reliability_failures"]
                 + 1.5 * len(p["classes"]) + p["repeated"] + p["pushed_back"], 2)


def compose(rows: list[dict], root: Path | None = None,
            min_failed: int = policy.BASH_RLE_MIN_FAILED_SHELL_CALLS) -> tuple[list[dict], list[dict]]:
    """(picked rows stamped for the track, best first; skipped [{seed_key, reason}])."""
    picked, skipped = [], []
    for row in rows:
        if not (row.get("classification") or {}).get("survives"):
            continue
        key = row.get("seed_key") or f"{row.get('session_id')}::{row.get('seed_index')}"
        try:
            trace = rec.load_trace(row["session_id"], root)
        except (OSError, ValueError, KeyError) as exc:
            skipped.append({"seed_key": key, "reason": f"trace unreadable: {type(exc).__name__}"})
            continue
        try:
            outcomes = sig.load_outcomes(row["session_id"], root)
        except (OSError, ValueError):
            outcomes = None
        p = profile(trace, outcomes, row.get("span"))
        if p["decision_failures"] < min_failed:
            skipped.append({"seed_key": key, "reason": f"{p['decision_failures']} failed shell decision(s) in the "
                                                       f"span, fewer than {min_failed}"})
            continue
        picked.append({**row, "track": TRACK, "track_priority": score(p), "bash_profile": p})
    picked.sort(key=lambda r: (r.get("tier", 4), -r["track_priority"], r.get("seed_key") or ""))
    return picked, skipped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--classified", default=str(ARTIFACTS / "classified.json"))
    ap.add_argument("-o", "--out", default=str(ARTIFACTS / "bash-rle-wave.json"))
    ap.add_argument("-n", "--count", type=int, default=None, help="keep the best N seeds (default: all that qualify)")
    ap.add_argument("--min-failed", type=int, default=policy.BASH_RLE_MIN_FAILED_SHELL_CALLS)
    args = ap.parse_args(argv)
    doc = json.loads(Path(args.classified).read_text())
    picked, skipped = compose(doc["rows"], sessions_root(), args.min_failed)
    if args.count is not None:
        picked = picked[:args.count]
    out = {"schema": doc.get("schema"), "model": doc.get("model"), "track": TRACK,
           "selected_from": str(args.classified), "min_failed_shell_decisions": args.min_failed,
           "rows": picked, "skipped": skipped}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"bash RLE wave: {len(picked)} seed(s) picked, {len(skipped)} skipped -> {args.out}")
    for rank, row in enumerate(picked, 1):
        p = row["bash_profile"]
        print(f"  {rank:>3}. tier {row.get('tier', '?')} {row.get('seed_key')} score {row['track_priority']}: "
              f"{p['decision_failures']} failed decision(s), Arena {p['arena_bash_reliability_failures']}, "
              f"classes {p['classes']}, missing {p['missing_programs']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
