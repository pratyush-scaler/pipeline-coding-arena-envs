"""Opt-in independent review: a hidden test that contradicts the published rule must be rejected.

The synthetic task's instruction says months are listed in chronological
order and its hidden test asserts exactly that. This driver flips the
INSTRUCTION (and the check's verbatim rule_quote, so static validation stays
clean) to "reverse chronological order (latest month first)" while the test
and the reference still sort ascending. Every executed control still passes --
the reference agrees with the tests -- so only a reviewer that reads the
instruction against the tests can find that `o_chronological_order_across_years`
enforces a rule the instruction does not publish. It must.
"""
import argparse
import json
from pathlib import Path

from pipeline.artifacts import atomic_json
from pipeline.author.build import build
from pipeline.cli import load_keys
from pipeline.review import review
from pipeline.tests.synthetic_code import write_bundle

PUBLISHED = "The report must key each month as `YYYY-MM` and list months in chronological order."
FLIPPED = ("The report must key each month as `YYYY-MM` and list months in reverse chronological "
           "order, the latest month first.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", default="gpt-6-astra")
    args = ap.parse_args()
    root = args.out.resolve()
    root.mkdir(parents=True, exist_ok=False)
    bundle = write_bundle(root / "task")
    spec = json.loads((bundle / "authoring/spec.json").read_text())
    spec["_producer"] = {"model": "claude-opus-5", "effort": "xhigh"}
    flipped = 0
    for step in spec["steps"]:
        if PUBLISHED in step["instruction"]:
            step["instruction"] = step["instruction"].replace(PUBLISHED, FLIPPED)
            flipped += 1
    for check in spec["checks"]:
        quotes = check.get("rule_quote") or []
        if PUBLISHED in quotes:
            check["rule_quote"] = [FLIPPED if q == PUBLISHED else q for q in quotes]
            flipped += 1
    if not flipped:
        raise SystemExit("the synthetic instruction no longer carries the published rule; update this driver")
    # The review verifies provenance against the trace, so the synthetic task
    # gets a synthetic dump: one session whose write_file calls carry exactly
    # the fixture bytes, making every file `lifted` for a real ledger.
    import os
    fixture = bundle / "environment/repo"
    trace = {"session_id": "session", "messages": [{"role": "assistant", "content": [
        {"type": "tool-write_file", "input": {"path": str(p.relative_to(fixture)), "content": p.read_text()}}
        for p in sorted(fixture.rglob("*")) if p.is_file()]}]}
    atomic_json(root / "dump/sessions/session/data.json", {"traces": [trace]})
    os.environ["ARENA_DUMP"] = str(root / "dump")
    spec["source"] = {"session_id": "session", "seed_index": 0, "tier": 4}
    build(spec, bundle)
    from pipeline.ledger import faithfulness
    from pipeline import naming
    ledger = faithfulness.with_level(bundle, faithfulness.build(bundle, "session", 0))
    atomic_json(bundle / "provenance/ledger.json", ledger)
    naming.apply(bundle, ledger["faithfulness_level"])
    print("ledger level", ledger["faithfulness_level"], "fidelity", ledger["faithfulness_by_bytes"], flush=True)
    load_keys(prompt_if_missing=False)
    report = review(bundle, args.model)
    doc = json.loads((bundle / report["work_dir"] / "deliverables/review.json").read_text())
    rows = {r.get("id"): r for r in doc.get("checks", [])}
    caught = rows.get("o_chronological_order_across_years", {}).get("verdict") not in (None, "pass")
    expected = ("semantic reviewer requested revision", "unresolved review findings:")
    valid = all(error.startswith(expected) or ": check not independently justified" in error
                for error in report["errors"])
    result = {"passed": caught and not report["ok"] and valid, "verdict": doc.get("verdict"),
              "execution_evidence_valid": valid, "caught_contradicting_test": caught,
              "flagged": sorted(cid for cid, r in rows.items() if r.get("verdict") != "pass"),
              "reason": rows.get("o_chronological_order_across_years", {}).get("reason"),
              "errors": report["errors"], "work_dir": report["work_dir"]}
    atomic_json(root / "validation.json", result)
    print(json.dumps(result, indent=1), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
