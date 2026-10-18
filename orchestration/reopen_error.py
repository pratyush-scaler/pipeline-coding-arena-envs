"""Grant one repair pass to a build that ended in `error` at the controls stage because a patch variant did not
apply (a design defect, not infrastructure). Mirrors pipeline.reopen's mechanics; the finding is the error itself
plus the chain rule. usage: orchestration/reopen_error.py <bundle> <why>"""
import sys, time
from pathlib import Path
from pipeline import checkpoint, repair
from pipeline.reopen import GUIDANCE
from pipeline.artifacts import atomic_json
from pipeline.author.contracts import strict_json

b, why = Path(sys.argv[1]).resolve(), sys.argv[2]
job = strict_json((b / "verification/state.json").read_text())
err = str(job.get("error") or "")
LEDGER_MISSING = job.get("stage_reached") == "ledger" and "FileNotFoundError" in err and "environment/repo" in err
if job.get("status") != "error" or not ("did not apply" in err or LEDGER_MISSING):
    sys.exit(f"skip: status {job.get('status')}: {err[:200]}")
state = checkpoint.read(b)
if not state.get("submitted"):
    sys.exit("skip: no submitted draft to revise")
checkpoint.submitted_spec(b, state)
findings = [f"ledger (pipeline, not your design): {err.strip()[:400]} -- a fixture file that extraction had redacted for "
            "privacy was removed from environment/repo; the ledger now records such removals. Resubmit the same draft "
            "unchanged unless a check you run fails."] if LEDGER_MISSING else [f"controls: {err.strip()} -- the alternative control applies authoring/alt/<step>.patch IN ORDER, so a "
            "later step's alternative must be a diff against the tree the EARLIER ALTERNATIVES leave (a later "
            "step's mutant: against the tree the earlier steps' references leave). Rewrite the failing patch "
            "against that tree and run_controls before submitting; change nothing else."]
log = repair.history(b)
log.setdefault("grants", []).append({"at": time.time(), "passes": 1, "by": why, "status_before": job.get("status"),
                                     "stage_before": state.get("stage")})
atomic_json(b / repair.LOG, log)
repair.record_pass(b, findings, "granted", {"phase": "gate", "granted_by": why})
checkpoint.update(b, stage="repair", feedback=[GUIDANCE, *findings], gate_repairs_remaining=0)
print("reopened:", findings[0][:200])
