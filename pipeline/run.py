"""Gate-chain driver for coding bundles.

  pre-calibration   everything checkable by reading the bundle and its
                    executed receipts (controls, probes)
  post-calibration  everything that needs measured episodes

Exit codes: 0 green; 1 blocked (a finding about the bundle); 2 unverified (a
gate errored or was skipped -- a finding about our ability to check).
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .gates import calibration, code
from .gates.base import GateContext, GateResult, Status, run_gates

PRE_CALIBRATION = code.CHAIN
POST_CALIBRATION = (calibration.bands_gate, calibration.validity_gate)


def verdict(results: list[GateResult]) -> tuple[str, int]:
    if any(r.status is Status.ERROR for r in results):
        return "unverified", 2
    if any(r.status is Status.SKIPPED for r in results):
        return "unverified", 2
    if any(r.status is Status.FAIL for r in results):
        return "blocked", 1
    return "green", 0


def run_bundle(task_dir: Path, repo_root: Path, calibration_report: dict | None = None, legacy: bool = False) -> dict:
    gates = list(PRE_CALIBRATION)
    extras: dict = {}
    if calibration_report is not None:
        gates += list(POST_CALIBRATION)
        extras["calibration"] = calibration_report
    if legacy:
        extras["legacy"] = True
    ctx = GateContext(task_dir=Path(task_dir), repo_root=Path(repo_root), extras=extras)
    results = run_gates(gates, ctx)
    state, code_ = verdict(results)
    return {"schema": "arena-pipeline/gate-report/1", "task": Path(task_dir).name,
            "phase": ("post-calibration" if calibration_report else "pre-calibration") + (" (legacy diagnostic)" if legacy else ""),
            "verdict": state, "exit_code": code_,
            "results": [{"gate": r.gate, "status": r.status.value,
                         "blockers": [asdict(f) for f in r.blockers], "warnings": [asdict(f) for f in r.warnings],
                         "evidence": r.evidence, "error": r.error, "duration_s": round(r.duration_s, 3)}
                        for r in results]}


def _print(report: dict, verbose: bool) -> None:
    print(f"\n=== {report['task']} [{report['phase']}] ===")
    for r in report["results"]:
        mark = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIP", "error": "ERR "}[r["status"]]
        extra = f"  {len(r['blockers'])} blocker(s)" if r["blockers"] else (f"  {len(r['warnings'])} warning(s)" if r["warnings"] else "")
        print(f"  {mark}  {r['gate']:28s}{extra}")
        if r["error"]:
            print(f"          ERROR: {r['error']}")
        for f in r["blockers"]:
            print(f"          BLOCKER {f['code']}: {f['message']}" + (f" [{f['where']}]" if f["where"] else ""))
        if verbose:
            for f in r["warnings"]:
                print(f"          warn    {f['code']}: {f['message']}" + (f" [{f['where']}]" if f["where"] else ""))
    print(f"  -> {report['verdict'].upper()}")


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Run the gate chain over coding bundles")
    ap.add_argument("tasks", nargs="+")
    ap.add_argument("--calibration", help="calibration report JSON (enables the post-calibration gates)")
    ap.add_argument("--json")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--legacy", action="store_true")
    args = ap.parse_args(argv)
    calibration_report = json.loads(Path(args.calibration).read_text()) if args.calibration else None
    repo_root = Path(__file__).resolve().parent.parent
    reports, worst = [], 0
    for raw in args.tasks:
        task_dir = Path(raw).resolve()
        if not task_dir.is_dir():
            print(f"  ERR  {raw}: not a directory")
            worst = max(worst, 2)
            continue
        report = run_bundle(task_dir, repo_root, calibration_report, args.legacy)
        reports.append(report)
        _print(report, args.verbose)
        worst = max(worst, report["exit_code"])
    if args.json:
        Path(args.json).write_text(json.dumps(reports, indent=2))
    print(f"\n{sum(1 for r in reports if r['verdict'] == 'green')}/{len(reports)} green")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
