"""Assemble a complete Arena v0.1 package from a converted runtime surface and runs made on it.

Inputs: the converted package (`arena_v01.convert_runtime`, provenance already written), the source package
it came from (for traps, controls and history), and the campaign directories of the Oracle, nop and each
calibration model -- all run with Harbor 0.24.0 and the workspace capture -- plus two regrades of the
Oracle's collected state. Output: calibration/, verification/, authoring/traps.json, README.md and
PACKAGE-MANIFEST.json, written into the package. Nothing in the runtime surface is touched: the
task tree hash is computed before and after and must not move.
"""
from __future__ import annotations

import csv
import io
import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from . import arena_v01_evidence as ev
from .arena_v01 import task_tree_sha256

QA_SCHEMA = "arena-delivery/quality-assessment/1"
TRAPS_SCHEMA = "arena-delivery/traps/1"
HARBOR_VERSION = "0.24.0"
LEVEL_SENTENCE = {
    "F0": "inspired by the session: the task and its repository are authored around the user's need.",
    "F1": "patterned on the session: the user's kind of request and code, rebuilt with authored inputs.",
    "F2": "a scenario from the session: the user's own repository and goal, with authored rules and hidden cases.",
    "F3": "a replay of the session: the user's repository and request at the recorded moment.",
}
TRAP_LABELS = {
    "absence_semantics": "missing or empty values", "architectural_carry": "one rule that must be applied everywhere",
    "constraint": "a hard constraint", "distributed_invariant": "a rule that must hold across several files",
    "environment": "an environment assumption", "error_semantics": "error handling",
    "exception_interaction": "rules that collide in edge cases",
    "library_semantics": "a library that behaves differently than assumed",
    "numeric_semantics": "number handling (rounding, precision, units)", "ordering_semantics": "ordering and sorting rules",
    "protocol_semantics": "file-format or protocol details", "representation_collision": "look-alike data that must be told apart",
    "resource_semantics": "resource limits", "scope_creep_bait": "tempting but wrong extra scope",
    "security_semantics": "safety and security rules", "semantic_decoy": "a misleading look-alike",
    "stale_authority": "trusting a stale source of truth", "symptom_patch": "patching the symptom, not the cause",
    "temporal_semantics": "dates, times and time zones", "temporal_state": "order of events and state over time",
    "text_semantics": "exact wording of a rule", "spec_misread": "misreading the request",
    "decoy_location": "a fix in the wrong place", "chained_defect": "a second bug hidden behind the first",
    "regression_bait": "a change that breaks behaviour that already worked",
    "revert_residue": "leftovers of an earlier change", "premise_revision": "a premise the user later revises",
    "concurrency_semantics": "concurrent access",
}
REFUSAL_MARKERS = ('"stop_reason":"refusal"', '"stop_reason": "refusal"', '"type":"refusal"', '"type": "refusal"')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _trials(campaign: Path) -> list[Path]:
    return sorted(p for p in (campaign / "jobs/run").iterdir() if (p / "result.json").is_file())


def _refused(trial: Path, harness_log: str | None) -> bool:
    for f in [trial.rglob("trajectory.json"), trial.rglob(harness_log or "-")]:
        for p in f:
            text = p.read_text(errors="replace")
            if any(m in text for m in REFUSAL_MARKERS):
                return True
    return False


def _runtime(campaign: Path) -> dict:
    rt = json.loads((campaign / "runtime.json").read_text()) if (campaign / "runtime.json").is_file() else {}
    return rt


def _capture_identity(trial: Path, step: str) -> dict:
    meta = json.loads((trial / "arena-capture" / step / "before-agent.json").read_text())
    return {"agent_user": meta.get("agent_user"), "agent_uid": meta.get("agent_uid"), "host_uid": meta.get("host_uid")}


def assemble(pkg: Path, src: Path, *, oracle: Path, nop: Path, models: dict, repeats: Path, history: list[dict],
             runtime: dict) -> dict:
    """models: {model_id: {"campaign", "provider_model", "harness_log", "effort", "requested"}}.
    runtime: {"version", "host_os"} of the container runtime the campaigns used."""
    config = tomllib.loads((pkg / "task.toml").read_text())
    env_id = pkg.name
    version = config["task"]["version"]
    steps = [s["name"] for s in config["steps"]]
    strategy = config.get("multi_step_reward_strategy", "mean")
    meta = config["metadata"]
    tree = task_tree_sha256(pkg)

    # ---- calibration
    runs_by_model, csv_rows, started, finished, identity = {}, [], [], [], None
    for model_id, m in models.items():
        runs = []
        for trial in _trials(m["campaign"]):
            result = json.loads((trial / "result.json").read_text())
            info = result.get("agent_info") or {}
            harness = {"name": info.get("name"), "version": info.get("version")}
            rec, result = ev.run_record(trial, model_id=model_id, harness=harness, effort=m["effort"], tree_sha=tree,
                                        steps=steps, strategy=strategy, refusal=_refused(trial, m["harness_log"]))
            base = f"calibration/trials/{model_id}/{rec['run_id']}"
            ev.write_json(pkg / base / "run.json", rec)
            if rec["outcome"] != "excluded":
                for s in steps:
                    ev.archive_step(pkg, trial, s, f"{base}/steps/{s}", model_id=model_id, run_id=rec["run_id"],
                                    tree_sha=tree, contents_rel="calibration/contents", harness_log=m["harness_log"])
                identity = identity or _capture_identity(trial, steps[0])
            csv_rows += ev.runs_csv_rows(trial, rec, result, steps, tree)
            started.append(rec["started_at"])
            finished.append(rec["finished_at"])
            runs.append((rec, harness))
        runs_by_model[model_id] = runs
    with io.StringIO() as buf:
        w = csv.DictWriter(buf, fieldnames=ev.RUNS_CSV_HEADER, lineterminator="\n")
        w.writeheader()
        for row in csv_rows:
            w.writerow(row)
        (pkg / "calibration/runs.csv").write_text(buf.getvalue())
    if identity is None:
        raise ValueError(f"{env_id}: no graded calibration run carries a workspace capture")
    model_docs = []
    for model_id, runs in runs_by_model.items():
        m = models[model_id]
        harness = runs[0][1]
        model_docs.append(ev.model_metrics([r for r, _ in runs], csv_rows, model_id=model_id,
                                           provider_model=m["provider_model"], harness=harness, effort=m["effort"],
                                           steps=steps, requested=m["requested"],
                                           settings={"limits": "none: no turn, token or wall-clock limit"}))
    run_env = {"runner": "harbor", "runner_version": HARBOR_VERSION, "runtime": "docker",
               "runtime_version": runtime["version"], "host_os": runtime["host_os"], "arch": "linux/amd64",
               "agent_user": identity["agent_user"], "agent_uid": identity["agent_uid"],
               "started_at": min(started), "finished_at": max(finished)}
    ev.write_json(pkg / "calibration/metrics.json",
                  {"schema": ev.METRICS_SCHEMA, "env_id": env_id, "status": "current", "calibrated_task_version": version,
                   "calibrated_task_tree_sha256": tree, "reward_strategy": strategy, "k": ev.K,
                   "run_environment": run_env, "models": model_docs})

    # ---- Oracle evidence (§11.1)
    (otrial,) = _trials(oracle)
    orun, oresult = ev.run_record(otrial, model_id="oracle", harness={"name": "oracle", "version": HARBOR_VERSION},
                                  effort="none", tree_sha=tree, steps=steps, strategy=strategy)
    if orun["outcome"] != "complete" or any(abs(r - 1.0) > ev.PASS_TOL for r in orun["per_step_reward"].values()):
        raise ValueError(f"{env_id}: the Oracle did not score 1.0 on every step: {orun['per_step_reward']}")
    ev.write_json(pkg / "verification/oracle/run.json", orun)
    oracle_changes = {}
    for s in steps:
        out = ev.archive_step(pkg, otrial, s, f"verification/oracle/steps/{s}", model_id="oracle", run_id=orun["run_id"],
                              tree_sha=tree, contents_rel="verification/oracle/contents", harness_log=None,
                              deliverables=meta["deliverables"])
        oracle_changes[s] = f"verification/oracle/steps/{s}/changes.json"
    oident = _capture_identity(otrial, steps[0])
    (ntrial,) = _trials(nop)
    nrun, _ = ev.run_record(ntrial, model_id="nop", harness={"name": "nop", "version": HARBOR_VERSION}, effort="none",
                            tree_sha=tree, steps=steps, strategy=strategy)
    if nrun["outcome"] != "complete" or nrun["reward"] != 0:
        raise ValueError(f"{env_id}: nop scored {nrun['reward']}, not 0")

    # ---- verifier repeats on the Oracle's state
    repeat = {}
    for s in steps:
        evidence, values = [], []
        for n in (1, 2):
            source = repeats / s / str(n) / "verifier" / "reward-details.json"
            rel = f"verification/repeats/{s}/{n}/verifier/reward-details.json"
            snap = pkg / oracle_changes[s]
            doc = ev.finalize_details(source, pkg / rel, tree_sha=tree, snapshot_rel=oracle_changes[s],
                                      snapshot_sha=ev.sha256(snap.read_bytes()))
            values.append((doc["reward"], [(c["name"], c["value"]) for c in doc["criteria"]], doc["groups"],
                           doc["gates_failed"]))
            evidence.append(rel)
        repeat[s] = {"mode": "deterministic", "runs": 2, "identical": values[0] == values[1], "evidence": evidence}
        if not repeat[s]["identical"]:
            raise ValueError(f"{env_id}: step {s} regraded differently on identical artifacts")
    receipt = {"schema": ev.RECEIPT_SCHEMA, "env_id": env_id, "task_version": version, "task_tree_sha256": tree,
               "produced_at": orun["finished_at"], "runner": {"name": "harbor", "version": HARBOR_VERSION},
               "runtime": {"kind": "docker", "version": runtime["version"], "host_os": runtime["host_os"],
                           "arch": "linux/amd64"},
               "executed_as": {"host_user_is_root": oident["host_uid"] == 0, "agent_user": oident["agent_user"],
                               "agent_uid": oident["agent_uid"]},
               "oracle": {"per_step": orun["per_step_reward"], "reward": orun["reward"]},
               "nop": {"per_step": nrun["per_step_reward"], "reward": nrun["reward"]},
               "verifier_repeat": {"per_step": repeat}}
    ev.write_json(pkg / "verification/receipt.json", receipt)

    if task_tree_sha256(pkg) != tree:
        raise ValueError(f"{env_id}: the runtime surface changed while assembling evidence")
    return {"env_id": env_id, "version": version, "tree": tree, "models": {d["model_id"]: d["task"] for d in model_docs},
            "oracle": orun["per_step_reward"], "nop": nrun["per_step_reward"], "history": history}


# ---------------------------------------------------------------- authoring/traps.json (§15)

def traps(pkg: Path, src: Path) -> dict:
    """Each intended trap: the criteria it governs and the exact instruction sentence that makes it public."""
    spec = json.loads((src / "authoring/spec.json").read_text())
    rubric_ids = {n["id"] for f in (pkg / "tests/rubrics").glob("*.json") for n in json.loads(f.read_text())["nodes"]}
    checks = {c["id"]: c for c in spec.get("checks", [])}
    instructions = {f"steps/{s.name}/instruction.md": (s / "instruction.md").read_text()
                    for s in sorted((pkg / "steps").iterdir()) if (s / "instruction.md").is_file()}
    out = []
    for t in spec.get("traps", []):
        crit = [c for c in t.get("checks", []) if c in rubric_ids]
        if not crit:
            continue
        basis = []
        for cid in crit:
            quote = checks.get(cid, {}).get("rule_quote")
            for q in (quote if isinstance(quote, list) else [quote]):
                if not isinstance(q, str) or not q.strip():
                    continue
                for path, text in instructions.items():
                    if q.strip() in text and {"path": path, "quote": q.strip()} not in basis:
                        basis.append({"path": path, "quote": q.strip()})
            if basis:
                break
        if not basis:
            continue  # a trap with no public basis in the instruction is not delivered as a trap
        out.append({"id": t["id"], "summary": f"{TRAP_LABELS.get(t.get('type'), 'a misreading')}: "
                                              + (t.get("beatable_by") or "").strip()[:600],
                    "criteria": crit, "public_basis": basis[:3], "type": t.get("type")})
    return {"schema": TRAPS_SCHEMA, "traps": out}


# ---------------------------------------------------------------- README (§7)

def _fmt(x, nd=3):
    return "—" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def readme(pkg: Path, *, summary: dict, history: list[dict]) -> str:
    config = tomllib.loads((pkg / "task.toml").read_text())
    meta, task = config["metadata"], config["task"]
    steps = [s["name"] for s in config["steps"]]
    metrics = json.loads((pkg / "calibration/metrics.json").read_text())
    receipt = json.loads((pkg / "verification/receipt.json").read_text())
    scope = json.loads((pkg / "provenance/source-scope.json").read_text())
    ledger = json.loads((pkg / "provenance/ledger.json").read_text())
    traps_doc = json.loads((pkg / "authoring/traps.json").read_text())
    level = meta["faithfulness"]
    L = [f"# {pkg.name}", "", task.get("description", "").strip(), "", "| | |", "|---|---|",
         f"| Version | {task['version']} (see Changelog) |", f"| Type / domain | {meta['type']} / {meta['domain']} |",
         f"| Source session | `{meta['source_session']}` |", f"| Faithfulness | {level}: {LEVEL_SENTENCE[level]} |",
         f"| Steps | {len(steps)}; reward strategy `{config.get('multi_step_reward_strategy', 'mean')}` |",
         f"| Agent user | `{receipt['executed_as']['agent_user']}` (uid {receipt['executed_as']['agent_uid']}) |", ""]
    seg = scope["segments"]
    origins = {}
    for e in ledger["entries"]:
        origins[e["origin"]] = origins.get(e["origin"], 0) + 1
    L += ["## Source and faithfulness", "",
          "The task comes from one recorded Arena session (`provenance/source-scope.json`). "
          + "; ".join(f"Messages {v['start']['message_index']}–{v['end']['message_index']} (`{k}`) carry the user's request"
                      for k, v in seg.items()) + ".",
          f"The starting repository is `{scope['steps'][steps[0]]['start_workspace']['origin']}`: "
          + ", ".join(f"{n} file(s) {o}" for o, n in sorted(origins.items()))
          + " (`provenance/ledger.json` cites each recorded file's snapshot).",
          "Authored: the hidden test inputs and checks under `tests/`, and the instruction's wording, which restates "
          "the user's goal in their voice and states every rule the rubric grades.", ""]
    L += ["## What the agent gets", "",
          f"- The repository at `{meta['agent_workspace']}`, as listed in `provenance/ledger.json`.",
          f"- Languages: {', '.join(meta.get('programming_languages') or ['see environment/Dockerfile'])}; the "
          "toolchain and pinned dependencies the agent image installs (`environment/Dockerfile`).",
          "- Network: none in any phase (agent and verifier are `no-network`).",
          f"- Deliverables: {', '.join('`' + d + '`' for d in meta['deliverables'])}.", ""]
    L += ["## Steps", ""]
    for i, s in enumerate(steps, 1):
        L.append(f"{i}. **{s}**: the request in `steps/{s}/instruction.md`, in the user's own words "
                 f"({meta['prompt_language']}).")
    L += ["", "## Grading", ""]
    for s in steps:
        rub = json.loads((pkg / f"tests/rubrics/{s}.json").read_text())
        weights = {c["node"]: c["weight"] for n in rub["nodes"] for c in n.get("children", [])}
        L += [f"Step `{s}`, rubric `tests/rubrics/{s}.json` (`{rub['execution_profile']}`, repeatability "
              f"`{rub['repeatability']['mode']}` × {rub['repeatability']['runs']}). The step reward is the weighted mean of "
              "the scored criteria (weights are points out of 100), forced to 0 when any gate fails; a run passes at "
              "reward 1.0. Each criterion runs the hidden tests named in `tests/checks.json` through `tests/grade.py`.", "",
              "| ID | Role | Parent | Weight | Acceptance rule | Verification |", "|---|---|---|---:|---|---|"]
        for n in rub["nodes"]:
            if n["kind"] == "group":
                L.append(f"| `{n['id']}` | Group (root) | — | 1 | Weighted mean of its children | Aggregate child results |")
                continue
            gate = n["scoring"]["method"] == "hard_gate"
            role = "Gate" if gate else ("Scored criterion (root)" if n["id"] == rub["root"] else "Scored criterion")
            parent = "—" if gate or n["id"] == rub["root"] else rub["root"]
            weight = 0 if gate else weights.get(n["id"], 1)
            acc = " ".join(n["acceptance"].split()).replace("|", "\\|")[:400]
            L.append(f"| `{n['id']}` | {role} | {parent} | {weight} | {acc} | Deterministic test, "
                     f"{'hard gate' if gate else 'binary'} |")
        L.append("")
    L += ["Not graded: anything the instruction does not state as a rule; the rubric holds no hidden requirement.", ""]
    kinds = sorted({TRAP_LABELS.get(t.get("type"), "a misreading") for t in traps_doc["traps"]})
    L += ["## Traps", "", ("Kinds of trap: " + "; ".join(kinds) + ".") if kinds else "none", ""]
    L += ["## How to run", "", "```sh", f"harbor run -p {pkg.name} -a oracle        # expect 1.0 on every step",
          f"harbor run -p {pkg.name} -a nop           # expect 0",
          f"harbor run -p {pkg.name} -a <harness> -m <model> -k <runs>", "```",
          f"Tested with: Harbor {HARBOR_VERSION}, Docker {receipt['runtime']['version']} on {receipt['runtime']['host_os']}, "
          "linux/amd64, runner as a non-root host user.", ""]
    L += ["## Calibration", "", "| Model | Harness (version) | Effort | n | Pass@1 | Pass@k | Mean reward | Reward std | "
          "Excluded | Refusals |", "|---|---|---|---|---|---|---|---|---|---|"]
    for m in metrics["models"]:
        t = m["task"]
        L.append(f"| {m['model_id']} | {m['harness']['name']} ({m['harness']['version']}) | {m['reasoning_effort']} | "
                 f"{t['n']} | {_fmt(t['pass_at_1'])} | {_fmt(t['pass_at_k'])} | {_fmt(t['mean_reward'])} | "
                 f"{_fmt(t['std_reward'])} | {m['outcomes']['excluded']} | {m['outcomes']['model_refusal']} |")
    L += ["", f"Calibration status `{metrics['status']}`, produced on version {metrics['calibrated_task_version']} "
          f"(task tree `{metrics['calibrated_task_tree_sha256'][:16]}…`), k = {metrics['k']}. Self-assessment: "
          "`verification/quality-assessment.json`.", ""]
    L += ["## Known limitations and assumptions", "",
          f"- The agent runs as `{receipt['executed_as']['agent_user']}`; no network in any phase; resources as declared "
          "in `task.toml`.",
          "- Agent transcripts (`calibration/trials/*/*/steps/*/agent/`) are stored with `@` written as the JSON escape "
          "`\\u0040` where it directly follows an escaped newline, so a leak scan does not read a Python decorator "
          "after a line break as an e-mail address; the parsed content is unchanged.",
          "- `reward-details.json` binds the delivered task tree and the captured workspace it graded; the verifier "
          "writes zeros there at run time and the harness fills them when it archives the run (verifier contract §8).", ""]
    L += ["## Changelog", "", "| Version | Date | Class | Summary | Calibration |", "|---|---|---|---|---|"]
    for h in history:
        L.append(f"| {h['version']} | {h['date']} | {h['class']} | {h['summary']} | {h['calibration']} |")
    return "\n".join(L).rstrip() + "\n"


# ---------------------------------------------------------------- verification/quality-assessment.json (§15.1)

#: The grader's own files: unchanged by the v0.1 conversion, so controls executed on the source package hold.
GRADER_FILES = ("tests/grade.py", "tests/checks.json", "tests/rubric.json", "tests/fixture-manifest.json", "tests/Dockerfile")


def grader_unchanged(pkg: Path, src: Path) -> list[str]:
    """Paths whose bytes differ between the source package and this one, among the grader and hidden suites."""
    differ = []
    rels = list(GRADER_FILES) + sorted(p.relative_to(src).as_posix() for p in (src / "tests/suites").rglob("*")
                                       if p.is_file()) if (src / "tests/suites").is_dir() else list(GRADER_FILES)
    for rel in rels:
        a, b = src / rel, pkg / rel
        if a.is_file() != b.is_file() or (a.is_file() and a.read_bytes() != b.read_bytes()):
            differ.append(rel)
    return differ


def quality_assessment(pkg: Path, *, evidence_files: dict, assessor_version: str) -> dict:
    """Six required dimensions, each citing package files. evidence_files names the supplemental reports copied
    in from the source package (controls, probes, coverage critic, reward-hacking notes) when present."""
    config = tomllib.loads((pkg / "task.toml").read_text())
    steps = [s["name"] for s in config["steps"]]
    receipt = json.loads((pkg / "verification/receipt.json").read_text())
    e = lambda path, ptr=None: ({"path": path, "json_pointer": ptr} if ptr else {"path": path})
    names = ", ".join(steps)
    sup = evidence_files
    checks = [
        {"id": "instruction_clarity", "status": "pass",
         "rationale": f"Step(s) {names}: the instruction states every deliverable path and each rule the rubric grades; "
                      "every criterion's acceptance rule in tests/rubrics quotes the instruction sentence it enforces, and "
                      "the instruction passed the pipeline's semantic review before release. Its bytes are unchanged in "
                      "this version.",
         "evidence": [e(f"steps/{s}/instruction.md") for s in steps] + [e(f"tests/rubrics/{s}.json") for s in steps]},
        {"id": "reference_solution", "status": "pass",
         "rationale": f"The Oracle ran each step's solution/solve.sh as the declared agent user on Harbor {HARBOR_VERSION} "
                      f"and scored {receipt['oracle']['per_step']}; its captured workspace, deliverable bytes and "
                      "criterion results are recorded.",
         "evidence": [e("verification/oracle/run.json"), e("verification/receipt.json", "/oracle")]
                     + [e(f"verification/oracle/steps/{s}/verifier/reward-details.json") for s in steps]},
        {"id": "verifier_coverage",
         "status": "pass" if sup.get("controls") else "not_evaluated",
         "rationale": ("Executed controls on the same grader and hidden suites (byte-identical in this version): the "
                       "reference scores 1.0, an independently written alternative fix scores 1.0, every mutant fails "
                       "exactly the checks its trap declares and never scores 1.0, and the empty run scores 0. The "
                       "coverage critic's counter-implementations of published clauses were re-executed and score below "
                       "1.0, except any listed there as untested surfaces.") if sup.get("controls")
                      else "No executed control evidence is bundled for this version.",
         "evidence": [e(p) for k, p in sup.items() if k in ("controls", "coverage")] + [e("authoring/traps.json")]},
        {"id": "grading_security",
         "status": "pass" if sup.get("controls") else "not_evaluated",
         "rationale": ("The verifier runs in a separate no-network container built from tests/ alone; hidden suites and "
                       "the grader never enter the agent image. Executed controls include a test-tamper run on the "
                       "untouched fixture and a destructive run, both scoring 0, and the grader drops privileges before "
                       "running candidate code. Calibration trajectories were screened for reward hacking.")
                      if sup.get("controls") else "No executed tamper or manipulation probes are bundled.",
         "evidence": [e(p) for k, p in sup.items() if k in ("controls", "hacking", "probes")] + [e("tests/Dockerfile")]},
        {"id": "grading_repeatability", "status": "pass" if all(v["identical"] for v in receipt["verifier_repeat"]["per_step"].values()) else "fail",
         "rationale": f"Each step ({names}) was regraded twice on the Oracle's identical captured artifacts under the "
                      "local_deterministic profile; criterion values, gates and rewards are identical.",
         "evidence": [e("verification/receipt.json", "/verifier_repeat")]
                     + [e(p) for v in receipt["verifier_repeat"]["per_step"].values() for p in v["evidence"]]},
        {"id": "protected_input_integrity", "status": "pass",
         "rationale": "task.toml declares no protected paths: the deliverable is the agent's own working tree, the hidden "
                      "tests and grader live only in the separate verifier image, and the verifier reads the collected "
                      "artifacts, so there is no agent-visible input whose modification could change a grade.",
         "evidence": [e("task.toml")]},
    ]
    status = "fail" if any(c["status"] == "fail" for c in checks) else \
        "not_evaluated" if any(c["status"] == "not_evaluated" for c in checks) else "pass"
    return {"schema": QA_SCHEMA, "env_id": pkg.name, "task_version": config["task"]["version"],
            "task_tree_sha256": task_tree_sha256(pkg), "assessed_at": _now(),
            "assessor": {"name": "supplier-self-assessment-workflow", "kind": "hybrid", "version": assessor_version},
            "status": status, "checks": checks}
