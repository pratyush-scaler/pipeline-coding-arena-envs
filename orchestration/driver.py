"""The batch driver: takes every configured batch from built tasks to shipped zips, unattended.

Each cycle probes every configured workspace for the batch trees in orchestration/config.json, then applies
fixed rules per lineage (newest version first):

  hard on both models (pilot pass@1 <= BAR each) and releasable  -> `shipfix <version> --release`
  release stopped on recorded blockers                          -> `harden <version> --release-repair`
  hack flags on a hard version                                  -> `hack-triage` (benign / genuine / unclear)
  not hard                                                      -> hidden-only rounds, then a full redesign
  release ready                                                 -> packaged into ship-zips/ and reproduced
  anything a human must decide                                  -> reported, never acted on

Nothing here edits a bundle. State: pipeline-artifacts/orchestration/driver.json; log: logs/driver.log.

usage: python orchestration/driver.py [--loop] [--dry-run] [--package] [--interval S]
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline.benchmark import coder  # noqa: E402
from orchestration import config as cfgmod  # noqa: E402

CFG = cfgmod.load()
STATE = cfgmod.STATE_DIR / "driver.json"
LOG = cfgmod.LOGS / "driver.log"
WORKSPACES = cfgmod.workspaces(CFG)
TREES = cfgmod.trees(CFG)
RELEASE_REPAIRS = 3           # release-repair rounds per lineage (user, 2026-10-18 09:4x)
MAX_FAST = 2                 # hidden-only harden rounds per lineage before a human looks
#: User, 2026-10-13: "Lets repair all the tasks and ensure all of the tasks are genuinely hard" (with Arena's Oct 12
#: learning-signal feedback). Each lineage's hardening budget is raised ONCE from the default 3 to this, explicitly
#: (`harden --attempt-budget`, every spent attempt retained); rounds past the first three are full redesigns, since
#: spreading difficulty over independent decisions may need new published rules.
EXTENDED_BUDGET = 5
MAX_RELEASES_PER_WS = 3      # 20 episodes each; ARENA_TRIAL_SLOTS caps trials per workspace anyway
MAX_TRIAGES_PER_CYCLE = 10   # each launch is a ~1 min coder ssh; one backlog (2026-10-18) was 39
BAR = 0.5                    # policy.STEP_PASS_RATE_MAX
SATURATED = 0.8              # both models at or above this pass@1: go to a full redesign after one hidden-only round


def _in_batch(lineage) -> bool:
    """A lineage of a configured batch: the full routing (triage, release repair, saturation redesign) applies."""
    return any(f"/{t}/" in str(lineage) for t in TREES)


def _ideas_file(row: dict, name: str) -> Path:
    """Per-lineage operator trap ideas (harden --traps), from the batch's trap_ideas folder when it has one."""
    tree = Path(row["lineage"]).parents[1].name
    folder = cfgmod.trap_ideas(tree, CFG)
    return (folder / f"{name}.json") if folder else Path("/nonexistent")

PROBE = r'''
import json, os, glob, sys
root = sys.argv[1]
alive = []
for run in glob.glob(os.path.join(root, "pipeline-artifacts/coder-runs/*")):
    try:
        pid = int(open(os.path.join(run, "pid")).read())
        os.kill(pid, 0)
    except Exception:
        continue
    try:
        argv = json.load(open(os.path.join(run, "cmd.json"))).get("argv") or []
    except Exception:
        argv = []
    alive.append({"run": os.path.basename(run), "argv": argv})
def load(p):
    try:
        return json.load(open(p))
    except Exception:
        return None
import hashlib, re
def _judge_retry(f):
    # pipeline.hack_triage.retryable: UNCLEAR only because a judge gave no verdict, within 3 triage runs.
    if f.get("verdict") != "unclear" or f.get("judge_runs", 1) >= 3:
        return False
    if "judge_error" in f:
        return bool(f["judge_error"])
    js = f.get("judges") or {}
    if f.get("tier") != 2 or f.get("guard_ok") is False or not js:
        return False
    failed = [j for j in js.values() if "error" in j or not re.search(r"VERDICT:\s*(BENIGN|GENUINE|UNCLEAR)", str(j.get("reasoning") or ""))]
    return bool(failed) and not [j for j in js.values() if j not in failed and j.get("verdict") == "UNCLEAR"]
def _hkey(h):
    return (h.get("trial"), h.get("step"), h.get("kind"), hashlib.sha256(str(h.get("evidence", "")).encode()).hexdigest())
def triage_state(v, scr):
    hits = [h for r in (scr.get("per_model") or {}).values() for h in (r.get("reward_hacking") or [])]
    def walk(o):
        if isinstance(o, dict):
            if o.get("trial") and o.get("kind") and "evidence" in o:
                hits.append(o); return
            for x in o.values(): walk(x)
        elif isinstance(o, list):
            for x in o: walk(x)
    walk(load(os.path.join(v, "verification/qualification.json")) or {})
    human = load(os.path.join(v, "verification/hack-adjudications.json")) or {}
    ruled = {(f.get("trial"), f.get("step"), f.get("kind"), f.get("evidence_sha256"))
             for f in (human.get("flags") or []) if human.get("adjudicator") == "human" and f.get("verdict") == "benign"}
    tri = load(os.path.join(v, "verification/hack-triage.json")) or {}
    seen = {tuple(f["key"]): ("retry" if _judge_retry(f) else f.get("verdict")) for f in tri.get("flags") or [] if f.get("key")}
    keys = {_hkey(h) for h in hits}
    return {"untriaged": sum(1 for k in keys if k not in ruled and seen.get(k, "retry") == "retry"),
            "benign": sum(1 for k in keys if seen.get(k) == "benign"), "genuine": sum(1 for k in keys if seen.get(k) == "genuine"),
            "unclear": sum(1 for k in keys if seen.get(k) == "unclear")}
trees = [t for t in (sys.argv[2] if len(sys.argv) > 2 else "").split(",") if t]
for lineage in sorted(p for t in trees for p in glob.glob(os.path.join(root, t, "target-*/c-*"))):
    if not os.path.isfile(os.path.join(lineage, "task.toml")) and not os.path.isdir(os.path.join(lineage, "verification")):
        pass
    versions = [lineage] + sorted(glob.glob(os.path.join(lineage, "hardened/v*")),
                                  key=lambda p: int(os.path.basename(p)[1:]) if os.path.basename(p)[1:].isdigit() else 0)
    # A revision every finished attempt left invalid is not a version: a failed repair pass leaves its fixture writes
    # behind without a rebuilt manifest, and screening it fails preflight as "stale" (2026-10-14 05:40, 9 lineages
    # such as c-028 v3). Shipfix versions and revisions no attempt names stay.
    _h = load(os.path.join(lineage, "hardened/hardening.json")) or {}
    _named, _valid = set(), set()
    for _a in _h.get("attempts") or []:
        if _a.get("revision") and _a.get("outcome"):
            _named.add(_a["revision"])
            if _a["outcome"] == "valid":
                _valid.add(_a["revision"])
    _valid |= {_s.get("revision") for _s in _h.get("shipfixes") or [] if isinstance(_s, dict)}
    versions = [v for v in versions if v == lineage or os.path.relpath(v, lineage) not in _named - _valid]
    rows = []
    for v in versions:
        scr = load(os.path.join(v, "verification/screen.json")) or {}
        rel = load(os.path.join(v, "verification/shipfix-release.json")) or {}
        st = load(os.path.join(v, "verification/state.json")) or {}
        rev = load(os.path.join(v, "verification/semantic-review.json"))
        sfx = load(os.path.join(v, "verification/shipfix.json"))
        rlog = load(os.path.join(v, "verification/repair-log.json")) or {}
        steps = [d for d in sorted(os.listdir(os.path.join(v, "steps")))] if os.path.isdir(os.path.join(v, "steps")) else []
        pm = {m: {"pass_at_1": r.get("pass_at_1"),
                  # release.qualify counts attribution voids the screen's episodes_valid does not (2026-10-13 c-001 v3:
                  # screen 5 valid, qualify "4 void episodes leave 1 valid"): take the smaller.
                  "valid": min(r.get("episodes_valid") or 0, (r.get("attribution") or {}).get("pass", 0)
                               + (r.get("attribution") or {}).get("fail", 0) + sum(v for k, v in (r.get("attribution") or {}).items()
                                                                                    if k not in ("void", "pass", "fail") and isinstance(v, int)))
                           if r.get("attribution") else r.get("episodes_valid"),
                  "requested": r.get("episodes_requested"), "mean": r.get("mean_reward")} for m, r in (scr.get("per_model") or {}).items()}
        fable = None
        if rel.get("ready"):
            nc = load(os.path.join(v, "verification/native-calibrations.json")) or {}
            for camp in nc.get("campaigns") or []:
                rep = load(os.path.join(camp, "report.json")) or {}
                if "fable" in str(rep.get("requested_model") or rep.get("model") or ""):
                    fable = camp
        rows.append({"path": v, "task": os.path.isfile(os.path.join(v, "task.toml")), "steps": steps, "fable_campaign": fable,
                     "status": st.get("status"), "stage": st.get("stage_reached"), "error": str(st.get("error") or "")[:600],
                     "session_id": st.get("session_id"), "seed_index": st.get("seed_index"), "blockers": (st.get("blockers") or "")[:400] if isinstance(st.get("blockers"), str) else st.get("blockers"),
                     "screen": pm, "screen_complete": scr.get("measurement_complete"), "step_rates": scr.get("step_pass_rates"),
                     "screen_artifact": scr.get("artifact_sha256"), "screen_exists": bool(scr),
                     "topup_exhausted": bool(scr.get("topup_exhausted")),
                     "hack_flags": sum(len(r.get("reward_hacking") or []) for r in (scr.get("per_model") or {}).values()),
                     "triage": triage_state(v, scr),
                     "has_signal": "signal" in scr, "signal_problems": scr.get("signal_problems"),
                     "decisive_types": (scr.get("signal") or {}).get("decisive_types"),
                     "review_ok": (rev or {}).get("ok") if rev is not None else None,
                     "shipfix_graded_ok": (sfx or {}).get("graded_ok") if sfx is not None else None,
                     "repair_passes": len(rlog.get("passes") or []),
                     "release": {"ready": rel.get("ready"), "steps": {k: {"ok": s.get("ok"), "error": (s.get("error") or "")[:300],
                                                                          "result": s.get("result")} for k, s in (rel.get("steps") or {}).items()}} if rel else None})
    hard = load(os.path.join(lineage, "hardened/hardening.json")) or {}
    print("@@" + json.dumps({"lineage": lineage, "versions": rows,
                             "harden_attempts": len(hard.get("attempts") or []),
                             "harden_status": hard.get("status"),
                             "max_attempts": hard.get("max_attempts", 3),
                             "busy": [a["run"] for a in alive if any(lineage in str(x) for x in a["argv"])],
                             "builds_alive": [a["run"] for a in alive if "build" in a["argv"]]}))
'''


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(line + "\n")


def probe(ws: str) -> list[dict]:
    # A Coder API blip can return empty output with exit 0 (2026-10-13 18:42: coding-5, ship-2 and ship-5 vanished
    # from a cycle): every configured workspace holds lineages, so an empty probe is retried, then reported.
    for attempt in range(3):
        try:
            out = coder.ssh(ws, f"python3 - {ROOT} {','.join(TREES)} <<'PYX'\n{PROBE}PYX", timeout=240, check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            log(f"{ws}: probe attempt {attempt + 1} failed ({type(exc).__name__})")
            time.sleep(20)
            continue
        rows = []
        for line in coder._clean(out.stdout.decode(errors="replace")).splitlines():
            if line.startswith("@@"):
                row = json.loads(line[2:])
                row["ws"] = ws
                rows.append(row)
        if rows:
            return rows
        time.sleep(20)
    log(f"{ws}: probe returned no lineages after 3 attempts")
    return []


def verdict(version: dict) -> tuple[str, list[str]]:
    """'hard' | 'not_hard' | 'incomplete' for a version's screen."""
    screen = version.get("screen") or {}
    # User, 2026-10-12: a pilot of 5 attempts per model first; the 10 x 2 calibration only once that pilot proves
    # the task hard. A pilot short of 5 valid episodes on either model proves nothing yet (voids are topped up).
    if len(screen) < 2 or any((r.get("valid") or 0) < 5 or r.get("pass_at_1") is None for r in screen.values()):
        return "incomplete", [f"{m.split('/')[-1]} valid={r.get('valid')}" for m, r in screen.items()]
    why = [f"{m.split('/')[-1]} pass@1 {r['pass_at_1']}" for m, r in screen.items() if r["pass_at_1"] >= BAR]
    for m, steps in (version.get("step_rates") or {}).items():
        why += [f"{m.split('/')[-1]} step {s} {v}" for s, v in (steps or {}).items() if v is not None and v > BAR]
    if why:
        return "not_hard", why
    # Arena spec Oct 12: hard is not enough; the pilot must carry a learning signal (pipeline.learning_signal).
    if not version.get("has_signal"):
        return "needs_signal", ["screen predates the learning-signal record"]
    # Reward spread is advisory (pipeline.learning_signal.advisories); screens from ba6e66b still list it as a problem.
    blocking = [p for p in version.get("signal_problems") or [] if "runs are flat" not in p]
    if blocking:
        return "not_hard", blocking
    return "hard", why


def launch(ws: str, argv: list[str], dry: bool) -> str | None:
    cmd = ["./arena", "coder", "run", ws, "--staged", "--no-sync", "--", *argv]
    if dry:
        log(f"DRY {ws}: {' '.join(argv)}")
        return None
    env = {k: v for k, v in __import__("os").environ.items() if k != "ANTHROPIC_AUTH_TOKEN"}
    for prefix, dump in TREE_DUMPS.items():
        if any(prefix in str(a) for a in argv):
            env["ARENA_DUMP"] = dump        # remote_env forwards the launcher's dump
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=1200, env=env)
    run = next((line.split()[-1] for line in r.stdout.splitlines() if "coder watch" in line), None)
    log(f"LAUNCH {ws}: {' '.join(argv)} -> {run or 'FAILED ' + (r.stdout + r.stderr)[-300:]}")
    return run


PACKAGE = "--package" in sys.argv


def package(ready_rows: list[dict], state: dict) -> None:
    """package_ready.py (spec audit, package_remote with TURNS.md for multi-turn, privacy, the packaging sweep,
    zip into ship-zips/), then the Arena reproduction gate on every new zip, in the background."""
    seen, batch = set(), []
    for r in ready_rows:
        if r["name"] in seen or not r.get("fable_campaign"):
            continue                                 # a name collision waits for the next cycle
        seen.add(r["name"])
        batch.append(r)
    if not batch:
        return
    tasks = cfgmod.STATE_DIR / f"package-{time.strftime('%Y%m%d-%H%M%S')}.json"
    tasks.write_text(json.dumps([{k: r[k] for k in ("ws", "bundle", "name", "fable_campaign")} for r in batch], indent=1))
    log(f"PACKAGE {len(batch)}: {[r['name'] for r in batch]}")
    env = {k: v for k, v in __import__("os").environ.items() if k != "ANTHROPIC_AUTH_TOKEN"}
    r = subprocess.run([str(ROOT / ".venv/bin/python"), str(ROOT / "orchestration/package_ready.py"), str(tasks)],
                       cwd=ROOT, capture_output=True, text=True, timeout=7200, env=env)
    log("PACKAGE output: " + (r.stdout + r.stderr)[-1500:].replace("\n", " | "))
    index = json.loads((ROOT / "ship-zips/index.json").read_text()) if (ROOT / "ship-zips/index.json").is_file() else {}
    zips = []
    for row in batch:
        got = (index.get("tasks") or {}).get(row["name"])
        if got and got.get("source") == row["bundle"]:
            state["lineages"][row["key"]]["packaged"] = got["zip"]
            zips.append(str(ROOT / "ship-zips" / got["zip"]))
        else:
            held = (index.get("held") or {}).get(row["name"])
            state["lineages"][row["key"]]["package_held"] = held
    if zips:
        out = ROOT / f"logs/repro-{time.strftime('%Y%m%d-%H%M%S')}.log"
        subprocess.Popen([str(ROOT / ".venv/bin/python"), str(ROOT / "orchestration/repro.py"), *zips],
                         cwd=ROOT, stdout=open(out, "w"), stderr=subprocess.STDOUT, env=env, start_new_session=True)
        log(f"REPRO launched for {len(zips)} zip(s) -> {out}")


WAVES = cfgmod.waves(CFG)
TREE_DUMPS = cfgmod.dumps(CFG)


def wave_rank(tree: str, session_id: str, seed_index) -> int | None:
    """The seed's 1-based rank in its wave file, ordered exactly as `build` orders survivors."""
    from pipeline.orchestrate import hardness_prior
    doc = json.loads((ROOT / "pipeline-artifacts/waves" / WAVES[tree]).read_text())
    rows = [r for r in doc["rows"] if r["classification"].get("survives") and r.get("tier", 4) <= 4]
    rows.sort(key=lambda r: (r.get("tier", 4), -hardness_prior(r), -r.get("dispute", 0)))
    for i, r in enumerate(rows, 1):
        if r["session_id"] == session_id and str(r["seed_index"]) == str(seed_index):
            return i
    return None


def error_repair(row: dict, version: dict, dry: bool) -> str | None:
    """A controls-stage `error` from an unapplied variant patch: grant one repair pass and relaunch that seed alone
    on the staged (current) code, which routes such defects to the designer."""
    lineage = Path(row["lineage"])
    tree = lineage.parents[1].name
    rank = wave_rank(tree, version.get("session_id"), version.get("seed_index"))
    if rank is None:
        log(f"error-repair {lineage.name}: rank not found")
        return None
    launch(row["ws"], ["orchestration/reopen_error.py", str(lineage),
                       "driver: controls error from an unapplied variant patch is a repairable design defect"], dry)
    time.sleep(60)
    return launch(row["ws"], ["build", "-o", str(lineage.parents[1]), "-c", str(ROOT / "pipeline-artifacts/waves" / WAVES[tree]),
                              "--ranks", str(rank), "--task-workers", "1", "--max-tier", "4"], dry)


MAX_SCREENS_PER_WS = 3      # pilots launched by this driver per workspace at once (Fable ITPM is shared)
#: Re-pilots per version. Raised from 2 on 2026-10-14: OPEN_AI_API_KEY1 was revoked and every Astra episode voided
#: (401) for hours, spending both re-pilots of many versions on infrastructure loss.
MAX_SCREEN_ATTEMPTS = 4


def _review_rejected(version: dict) -> bool:
    """Blocked by the semantic review (its findings are rendered `Semantic review ...`, or the grade said so)."""
    text = json.dumps(version.get("blockers") or []) + " " + str(version.get("error") or "")
    if version.get("status") is not None:
        # A build root: its CURRENT blockers decide. A semantic-review.json from an earlier repair pass outlives
        # the revision it judged (2026-10-13 c-011-snmp: blocked at gates after pass 2, stale review ok=false,
        # a pilot wasted and the repair refused for lack of current findings).
        return "Semantic review" in text or "semantic reviewer requested revision" in text
    return ("Semantic review" in text or "semantic reviewer requested revision" in text
            or version.get("review_ok") is False)


def _budget_args(row: dict) -> list[str]:
    """`--attempt-budget EXTENDED_BUDGET` once the default budget is spent (explicit, spent attempts retained)."""
    return (["--attempt-budget", str(EXTENDED_BUDGET)]
            if row.get("harden_attempts", 0) >= row.get("max_attempts", 3) and row.get("max_attempts", 3) < EXTENDED_BUDGET
            else [])


def _write_ledger(rows: list[dict]) -> None:
    """Decisive trap types over the batch's measured-hard versions -> waves/decisive-traps.json (pushed when it
    changes), which designers and hardening rounds read (pipeline.learning_signal.guidance)."""
    from pipeline import learning_signal
    signals = {}
    for row in rows:
        for v in reversed([v for v in row["versions"] if v.get("task")]):
            screen = v.get("screen") or {}
            if len(screen) == 2 and all((r.get("pass_at_1") is not None and r["pass_at_1"] < BAR) for r in screen.values()) \
                    and v.get("decisive_types"):
                signals[Path(row["lineage"]).name] = {"decisive_types": v["decisive_types"]}
                break
    ledger = learning_signal.batch_ledger(signals)
    path = ROOT / "pipeline-artifacts/waves/decisive-traps.json"
    old = path.read_text() if path.is_file() else ""
    new = json.dumps(ledger, indent=1)
    if json.loads(old or "{}").get("decisive_type_share") == ledger["decisive_type_share"]:
        return
    path.write_text(new)
    log(f"LEDGER decisive trap types: {ledger['decisive_type_share']} over-used {ledger['over_used']}")
    # Pushed DETACHED (2026-10-13 18:55: a synchronous push hung on an rsync with no timeout while Coder SSH was
    # dropping connections, and the driver ran no cycle for almost three hours). A failed push is retried at the next
    # ledger change; designers read whatever ledger their workspace holds.
    env = {k: v for k, v in __import__("os").environ.items() if k != "ANTHROPIC_AUTH_TOKEN"}
    env["PYTHONPATH"] = str(ROOT)
    out = ROOT / f"logs/ledger-push-{time.strftime('%Y%m%d-%H%M%S')}.log"
    subprocess.Popen([str(ROOT / ".venv/bin/python"), str(ROOT / "orchestration/push_artifacts.py"), *WORKSPACES], cwd=ROOT, env=env,
                     stdout=open(out, "w"), stderr=subprocess.STDOUT, start_new_session=True)


def _screen_attempts(entry: dict, path: str) -> int:
    return sum(1 for a in entry.get("actions") or [] if a["kind"] == "screen" and a.get("version") == path)


def _release_earlier_hard(row, entry, versions, releases_running, summary, name, dry) -> bool:
    """Budget spent on a lineage whose LATEST revision is not releasable: release the newest EARLIER revision whose
    pilot was hard (both models, every step), with no hack flags and no rejecting review or failed acceptance, that
    has not been released before (2026-10-13: later hardening attempts sometimes measured easier than an earlier
    hard one, which the latest-only rule never offered for release). qualify still reviews it first."""
    released = {a.get("version") for a in entry["actions"] if a["kind"] == "release"}
    for v in reversed(versions[:-1]):
        if v["path"] in released or v.get("hack_flags") or v.get("review_ok") is False \
                or v.get("shipfix_graded_ok") is False:
            continue
        if v is versions[0] and v.get("status") != "validated":
            continue
        kind, _ = verdict(v)
        if kind != "hard":
            continue
        if releases_running.get(row["ws"], 0) >= MAX_RELEASES_PER_WS:
            summary["hard_validated"].append(f"{name} {Path(v['path']).name} (earlier hard revision, waiting for a slot)")
            return True
        run = launch(row["ws"], ["shipfix", v["path"], "--release"], dry)
        entry["actions"].append({"kind": "release", "version": v["path"], "run": run, "at": time.time(), "earlier": True})
        releases_running[row["ws"]] = releases_running.get(row["ws"], 0) + 1
        summary["releasing"].append(f"{name} {Path(v['path']).name} (earlier hard revision)")
        return True
    return False


def cycle(dry: bool) -> dict:
    state = json.loads(STATE.read_text()) if STATE.is_file() else {"lineages": {}}
    rows = []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(WORKSPACES)) as pool:
        for got in pool.map(probe, WORKSPACES):
            rows += got
    releases_running = {}
    triages = [0]
    for row in rows:
        for v in row["versions"]:
            if v.get("release") is not None and v["release"].get("ready") is None and row["busy"]:
                releases_running[row["ws"]] = releases_running.get(row["ws"], 0) + 1
    screens_running: dict[str, int] = {}
    for row in rows:
        entry = state["lineages"].get(f"{row['ws']}:{Path(row['lineage']).name}") or {}
        if row["busy"] and any(a["kind"] == "screen" for a in (entry.get("actions") or [])[-1:]):
            screens_running[row["ws"]] = screens_running.get(row["ws"], 0) + 1
    summary = {"hard_validated": [], "releasing": [], "ready": [], "hardening": [], "blocked": [], "building": [], "not_hard_spent": []}
    ready_rows: list[dict] = []
    for row in rows:
        name = Path(row["lineage"]).name
        key = f"{row['ws']}:{name}"
        entry = state["lineages"].setdefault(key, {"ws": row["ws"], "lineage": row["lineage"], "actions": []})
        if not dry and _in_batch(row["lineage"]):
            # A launch that failed (no run id: 2026-10-18 12:37, c-030 v3's release repair died staging on a
            # 120 s timeout) launched nothing; recorded, it blocked every retry. Infrastructure loss is re-run.
            entry["actions"] = [a for a in entry["actions"] if a.get("run")]
        versions = [v for v in row["versions"] if v.get("task")]
        if not versions:
            summary["building"].append(name)
            continue
        latest = versions[-1]
        # A released version ANYWHERE in the lineage is the lineage's result (2026-10-18: c-015 v6, c-023 v6 and
        # c-038 v5 were ready while repair rounds on them ran, and only the latest version was ever packaged).
        # Packaged once; no further round is launched for the lineage.
        shipped = next((v for v in reversed(versions) if (v.get("release") or {}).get("ready")), None)
        if shipped is not None:
            summary["ready"].append(f"{name} {shipped['path']}")
            entry["ready"] = shipped["path"]
            if not entry.get("packaged"):
                ready_rows.append({"ws": row["ws"], "bundle": shipped["path"], "name": name,
                                   "fable_campaign": shipped.get("fable_campaign"), "key": key})
            continue
        if row["busy"] and any(a["kind"] in ("harden", "harden_full") for a in entry["actions"][-1:]) \
                and not any(a["kind"] == "release" for a in entry["actions"]):
            # A round keeps going past a version that is already hard on both models when the round's own target
            # is stricter (2026-10-18 c-019: v3 0.2 / 0.0, shippable, then v4 0.2 / 0.8 and a new round on v4).
            # Release that version now, once per lineage, while the round runs; qualify still reviews it.
            if _release_earlier_hard(row, entry, versions + [{}], releases_running, summary, name, dry):
                continue
        if row["busy"]:
            (summary["releasing"] if any(a["kind"] == "release" for a in entry["actions"][-1:]) else summary["hardening"]
             if any(a["kind"] in ("harden", "harden_full", "review_repair", "screen", "critic_close", "signal_fix", "release_repair", "hack_triage", "resummarize") for a in entry["actions"][-1:])
             else summary["building"]).append(name)
            continue
        rel = latest.get("release")
        if rel and rel.get("ready"):
            summary["ready"].append(f"{name} {latest['path']}")
            entry["ready"] = latest["path"]
            if not entry.get("packaged"):
                ready_rows.append({"ws": row["ws"], "bundle": latest["path"], "name": name,
                                   "fable_campaign": latest.get("fable_campaign"), "key": key})
            continue
        status = row["versions"][0].get("status") if latest is versions[0] else "validated"
        if latest is versions[0] and status in (None, "running"):
            summary["building"].append(f"{name} (designing)")
            continue
        err0 = versions[0].get("error") or ""
        if latest is versions[0] and status == "error" and ("did not apply" in err0 or (
                versions[0].get("stage") == "ledger" and "FileNotFoundError" in err0 and "environment/repo" in err0)) \
                and not any(a["kind"] == "error_repair" for a in entry["actions"]):
            run = error_repair(row, versions[0], dry)
            entry["actions"].append({"kind": "error_repair", "run": run, "at": time.time()})
            summary["hardening"].append(f"{name}: error-repair (unapplied variant patch)")
            continue
        # User 2026-10-18 09:4x: "repair these tasks properly ... the repair phase has the exact information of what
        # needs to be fixed ... fix the issues properly and at once" (32 lineages hard on both, 6 shipped). A version
        # hard on both models (pilot pass@1 <= BAR each, no hack flag) that release stopped -- or that only the
        # learning signal / per-step bar keeps from release, or a root build the readiness sweep blocked -- gets ONE
        # release-repair round carrying every recorded blocker (pipeline/release_blockers.py), once per version and
        # at most RELEASE_REPAIRS per lineage. The user's instruction is the explicit budget increase it needs.
        def _pass_hard(v):
            scr = v.get("screen") or {}
            return len(scr) >= 2 and all((r.get("valid") or 0) >= 5 and r.get("pass_at_1") is not None
                                         and r["pass_at_1"] <= BAR for r in scr.values())

        def _qualify_flags(v):
            q = (((v.get("release") or {}).get("steps") or {}).get("qualify") or {}).get("result") or {}
            return [e for e in q.get("errors") or [] if "reward hacking unresolved" in str(e)]

        # User 2026-10-18 ~18:40: reward-hacking flags on a hard version are triaged automatically
        # (pipeline/hack_triage.py): only proven false positives go, genuine ones are repaired, unclear ones wait
        # for a human. The newest hard version holding flags (screen or qualification) is handled first, then a
        # flagged latest version a hardening round refused (2026-10-18: 13 lineages sat "await human adjudication"),
        # so hardening continues past a false positive and a genuine flag is repaired instead of waiting.
        if True:
            flagged = next((v for v in reversed(versions) if _pass_hard(v) and (v.get("hack_flags") or _qualify_flags(v))
                            and not (v.get("release") or {}).get("ready")), None)
            spent = row.get("harden_attempts", 0) >= EXTENDED_BUDGET or sum(
                1 for a in entry["actions"] if a["kind"] in ("harden", "harden_full")) >= EXTENDED_BUDGET
            if flagged is None and latest.get("hack_flags") and not spent and any(
                    a["kind"] in ("harden", "harden_full") and a.get("version") == latest["path"] for a in entry["actions"]):
                flagged = latest
            if flagged is not None:
                tri = flagged.get("triage") or {}
                runs = [a for a in entry["actions"] if a["kind"] == "hack_triage" and a.get("version") == flagged["path"]]
                if tri.get("untriaged") and len(runs) < 3 and triages[0] >= MAX_TRIAGES_PER_CYCLE:
                    summary["hardening"].append(f"{name}: hack triage on {Path(flagged['path']).name} queued (next cycle)")
                    continue
                if tri.get("untriaged"):
                    if len(runs) < 3:
                        triages[0] += 1
                        run = launch(row["ws"], ["hack-triage", flagged["path"], "--resummarize"], dry)
                        entry["actions"].append({"kind": "hack_triage", "version": flagged["path"], "run": run, "at": time.time()})
                        summary["hardening"].append(f"{name}: triaging {tri.get('untriaged')} hack flag(s) on "
                                                    f"{Path(flagged['path']).name}")
                    else:
                        summary["blocked"].append(f"{name}: hack triage on {Path(flagged['path']).name} did not finish "
                                                  "after 3 runs; a human looks")
                    continue
                if tri.get("unclear"):
                    summary["blocked"].append(f"{name}: {tri['unclear']} hack flag(s) on {Path(flagged['path']).name} "
                                              "judged UNCLEAR -- a human ruling is needed")
                    continue
                if tri.get("genuine") and not _pass_hard(flagged):
                    # A genuine exploit on a version both models still solve: its grader is fixed by hand (the
                    # release repair that carries HACK blockers is for hard versions).
                    summary["blocked"].append(f"{name}: {tri['genuine']} GENUINE hack flag(s) on {Path(flagged['path']).name} "
                                              "(not hard) -- the grader needs a repair")
                    continue
                if not tri.get("genuine"):
                    # Every flag is decided benign: the stored screen still lists them until re-derived, and a
                    # qualification refused for them is re-run, once each.
                    if flagged.get("hack_flags") and not any(a["kind"] == "resummarize" and a.get("version") == flagged["path"]
                                                             for a in entry["actions"]):
                        run = launch(row["ws"], ["screen", "--include-unvalidated", "--label", "resummarize", flagged["path"]], dry)
                        entry["actions"].append({"kind": "resummarize", "version": flagged["path"], "run": run, "at": time.time()})
                        summary["hardening"].append(f"{name}: re-deriving {Path(flagged['path']).name} after benign triage")
                        continue
                    # Its own marker: a crash re-run of the same version (release_retry) must not use up this one
                    # (2026-10-19 c-012-react-web-app v4 sat 6 h with every qualification flag ruled benign).
                    if _qualify_flags(flagged) and not flagged.get("hack_flags") and not any(
                            a["kind"] == "release_after_triage" and a.get("version") == flagged["path"] for a in entry["actions"]):
                        run = launch(row["ws"], ["shipfix", flagged["path"], "--release"], dry)
                        entry["actions"].append({"kind": "release_after_triage", "version": flagged["path"], "run": run, "at": time.time()})
                        summary["releasing"].append(f"{name} {Path(flagged['path']).name} (re-released after benign triage)")
                        continue
        # A release step that CRASHED (an error, no result -- 2026-10-18 c-027-csv v5: qualify's preflight died on a
        # truncated trace copy) found nothing to repair; infrastructure loss is re-run, once per version.
        if _in_batch(row["lineage"]):
            crashed = next((v for v in reversed(versions) if _pass_hard(v) and not v.get("hack_flags")
                            and (v.get("release") or {}).get("ready") is False
                            and any(st.get("ok") is False and st.get("error") and not st.get("result")
                                    for st in ((v.get("release") or {}).get("steps") or {}).values())), None)
            if crashed is not None and crashed is versions[-1] and not any(
                    a["kind"] == "release_retry" and a.get("version") == crashed["path"] for a in entry["actions"]):
                run = launch(row["ws"], ["shipfix", crashed["path"], "--release"], dry)
                entry["actions"].append({"kind": "release_retry", "version": crashed["path"], "run": run, "at": time.time()})
                summary["releasing"].append(f"{name} {Path(crashed['path']).name} (release re-run after a crashed step)")
                continue
        repaired = {a.get("version") for a in entry["actions"] if a["kind"] == "release_repair"}
        candidate = None
        in_batch = _in_batch(row["lineage"])                  # elsewhere only a genuine hack is release-repaired
        for v in reversed(versions):
            tri_v = v.get("triage") or {}
            hacked = bool(v.get("hack_flags") or _qualify_flags(v))
            if not _pass_hard(v) or (v.get("release") or {}).get("ready") or (
                    hacked and not (tri_v.get("genuine") and not tri_v.get("untriaged") and not tri_v.get("unclear"))):
                continue
            rel_v = v.get("release") or {}
            blocked_root = v is versions[0] and status in ("blocked", "unverified")
            if (in_batch and (rel_v.get("ready") is False or verdict(v)[0] == "not_hard" or blocked_root)) \
                    or (hacked and tri_v.get("genuine")):
                candidate = v
            break
        n_repairs = sum(1 for a in entry["actions"] if a["kind"] == "release_repair" and a.get("run"))
        if candidate is not None and candidate["path"] in repaired and candidate is not versions[-1]:
            summary["blocked"].append(f"{name}: release repair on {Path(candidate['path']).name} already ran; "
                                      "its result is not hard-and-releasable")
            continue
        # A repair that left no valid revision (its attempts failed validation: 2026-10-18 c-025 v7, c-033 v7, c-030
        # v3) runs again from the same version, told why the last one failed (release_blockers._attempts); a lineage
        # the repair governor stopped is reopened for it (c-011-ocr, c-023), within RELEASE_REPAIRS.
        if candidate is not None and n_repairs < RELEASE_REPAIRS:
            spent = row.get("harden_attempts", 0)
            budget = ["--attempt-budget", str(spent + 2)] if spent >= row.get("max_attempts", 3) else []
            reopen = ["--reopen-stopped"] if row.get("harden_status") == "stopped" else []
            run = launch(row["ws"], ["harden", candidate["path"], "--apply", "--release-repair", *budget, *reopen], dry)
            entry["actions"].append({"kind": "release_repair", "version": candidate["path"], "run": run,
                                     "at": time.time(), "budget": budget})
            summary["hardening"].append(f"{name}: release repair of {Path(candidate['path']).name}")
            continue
        review_blocked = (latest is versions[0] and status in ("blocked", "unverified") and _review_rejected(versions[0])) \
            or (latest is not versions[0] and (latest.get("review_ok") is False or latest.get("shipfix_graded_ok") is False)
                and _review_rejected(latest))
        if review_blocked:
            # User, 2026-10-04 ("Fix all the tasks based on the review") and 2026-10-12 (hardening budget 3): a build
            # the semantic review rejected after its repair passes is repaired by a hardening round with
            # --review-repair, which needs a measured pilot first. Pilot (5 x 2, unvalidated), then repair.
            kind, why = verdict(latest)
            if kind == "incomplete":
                if _screen_attempts(entry, latest["path"]) >= 2:
                    summary["blocked"].append(f"{name}: review-blocked, pilot still incomplete after 2 attempts {why}")
                elif screens_running.get(row["ws"], 0) >= MAX_SCREENS_PER_WS:
                    summary["blocked"].append(f"{name}: review-blocked, waiting for a pilot slot")
                else:
                    run = launch(row["ws"], ["screen", *(["--relaunch-incomplete"] if latest.get("topup_exhausted") and not latest.get("hack_flags") else []),
                                             "--include-unvalidated", "--label", "review-baseline", latest["path"]], dry)
                    entry["actions"].append({"kind": "screen", "version": latest["path"], "run": run, "at": time.time()})
                    screens_running[row["ws"]] = screens_running.get(row["ws"], 0) + 1
                    summary["hardening"].append(f"{name}: pilot for review repair")
                continue
            if any(a["kind"] == "review_repair" and a.get("version") == latest["path"] for a in entry["actions"]):
                if _budget_args(row) and not latest.get("hack_flags") and not any(a.get("extended") for a in entry["actions"]):
                    run = launch(row["ws"], ["harden", latest["path"], "--apply", "--review-repair", *_budget_args(row)], dry)
                    entry["actions"].append({"kind": "review_repair", "version": latest["path"], "pilot": why, "run": run,
                                             "at": time.time(), "extended": True})
                    summary["hardening"].append(f"{name}: extended-budget review repair")
                    continue
                summary["blocked"].append(f"{name}: review repair on {Path(latest['path']).name} produced no new version")
                continue
            if row.get("harden_attempts", 0) >= EXTENDED_BUDGET:
                if _release_earlier_hard(row, entry, versions, releases_running, summary, name, dry):
                    continue
                summary["not_hard_spent"].append(f"{name}: review-blocked, hardening budget spent")
                continue
            run = launch(row["ws"], ["harden", latest["path"], "--apply", "--review-repair", *_budget_args(row)], dry)
            entry["actions"].append({"kind": "review_repair", "version": latest["path"], "pilot": why, "run": run,
                                     "at": time.time()})
            summary["hardening"].append(f"{name}: review repair (pilot {why or 'measured'})")
            continue
        infra_review = json.dumps(versions[0].get("blockers") or []) + str(versions[0].get("error") or "")
        if latest is versions[0] and status == "unverified" and any(k in infra_review for k in (
                "review deliverable is not valid JSON", "semantic review could not be verified", "OpenAI call failed",
                "semantic review unavailable")) and sum(a["kind"] == "review_rerun" for a in entry["actions"]) < 3:
            # The review itself failed (provider error, malformed deliverable): infrastructure loss is re-run, up to
            # three times (2026-10-18: c-029-python-cli-agent's re-run returned malformed JSON again).
            # `unverified` is not a terminal checkpoint, so relaunching the seed resumes at the review.
            tree = Path(row["lineage"]).parents[1].name
            rank = wave_rank(tree, versions[0].get("session_id"), versions[0].get("seed_index"))
            if rank is not None:
                run = launch(row["ws"], ["build", "-o", str(Path(row["lineage"]).parents[1]), "-c",
                                         str(ROOT / "pipeline-artifacts/waves" / WAVES[tree]), "--ranks", str(rank),
                                         "--task-workers", "1", "--max-tier", "4"], dry)
                entry["actions"].append({"kind": "review_rerun", "run": run, "at": time.time()})
                summary["hardening"].append(f"{name}: review re-run (infrastructure)")
                continue
        if latest is versions[0] and status != "validated":
            summary["blocked"].append(f"{name}: {status} {str(row['versions'][0].get('blockers'))[:120]} "
                                      f"{(versions[0].get('error') or '')[:120]}")
            continue
        if latest is not versions[0] and (latest.get("review_ok") is False or latest.get("shipfix_graded_ok") is False):
            summary["blocked"].append(f"{name}: {Path(latest['path']).name} failed acceptance (not a review finding)")
            continue
        kind, why = verdict(latest)
        gated = next((v for v in reversed(versions[:-1]) if (v.get("release") or {}).get("ready") is False
                      and [k for k, st in (v["release"].get("steps") or {}).items() if not st.get("ok")] == ["critic_gate"]
                      and not v.get("hack_flags")), None)
        if gated and kind != "hard" and not any(a["kind"] == "critic_close" and a.get("version") == gated["path"]
                                                 for a in entry["actions"]):
            # An EARLIER hard version (released first, see below) stopped at release on open ship-critic gaps while a
            # later round measured easier (2026-10-18 c-019: v3 0.2 / 0.0 gated, v4 0.2 / 0.8): close the gaps there.
            run = launch(row["ws"], ["harden", gated["path"], "--apply", "--fast", "--critic-gaps", *_budget_args(row)], dry)
            entry["actions"].append({"kind": "critic_close", "version": gated["path"], "run": run, "at": time.time()})
            summary["hardening"].append(f"{name}: closing ship-critic gaps on {Path(gated['path']).name} (earlier hard)")
            continue
        if kind == "not_hard" and not any(a["kind"] == "release" for a in entry["actions"]) \
                and _release_earlier_hard(row, entry, versions, releases_running, summary, name, dry):
            # An earlier version is hard on both models: release it before spending another round (once per lineage).
            continue
        if rel and rel.get("ready") is False:
            hardness = (rel["steps"].get("hardness") or {}).get("result") or {}
            not_hard = hardness.get("not_hard") or []
            others = [k for k, s in rel["steps"].items() if k != "hardness" and (not s.get("ok") or
                      (k == "genuine" and not (s.get("result") or {}).get("certified")) or
                      (k == "shipcheck" and (s.get("result") or {}).get("open")))]
            if others == ["critic_gate"] and not any(a["kind"] == "critic_close" and a.get("version") == latest["path"]
                                                     for a in entry["actions"]):
                # Release stopped before calibrating on open ship-critic gaps and names the route itself: a
                # hidden-only round fixing exactly those gaps on this version (2026-10-18: c-015 v2 at 0.0 / 0.0
                # and c-024 v2 held here). Once per version; the new version is piloted and released as usual.
                run = launch(row["ws"], ["harden", latest["path"], "--apply", "--fast", "--critic-gaps",
                                         *_budget_args(row)], dry)
                entry["actions"].append({"kind": "critic_close", "version": latest["path"], "run": run,
                                         "at": time.time()})
                summary["hardening"].append(f"{name}: closing ship-critic gaps on {Path(latest['path']).name}")
                continue
            if others:
                summary["blocked"].append(f"{name}: release not ready -- {others} {latest['path']}")
                continue
            if not_hard:
                kind, why = "not_hard", not_hard
        if kind == "needs_signal":
            # Re-derive the stored screen on current code (no new episodes) so it carries the learning signal.
            if not any(a["kind"] == "resummarize" and a.get("version") == latest["path"] for a in entry["actions"]):
                run = launch(row["ws"], ["screen", "--include-unvalidated", "--label", "resummarize", latest["path"]], dry)
                entry["actions"].append({"kind": "resummarize", "version": latest["path"], "run": run, "at": time.time()})
                summary["hardening"].append(f"{name}: re-deriving its pilot with the learning signal")
            else:
                summary["blocked"].append(f"{name}: pilot re-derived but still carries no learning signal")
            continue
        if kind == "incomplete":
            if latest.get("screen_exists") and latest.get("topup_exhausted") and latest.get("hack_flags"):
                # Top-up spent and relaunching would discard a campaign whose hack flags await a human: a plain
                # re-screen reuses the exhausted record and changes nothing (2026-10-14 03:00, node-whatsapp-bot v5).
                summary["blocked"].append(f"{name}: incomplete pilot holds {latest['hack_flags']} hack flag(s); "
                                          f"relaunch waits on human adjudication")
                continue
            # A pilot short of valid episodes with nothing running on it (a hardening round that exited on voids,
            # 2026-10-12 c-009-tkinter): infrastructure loss is re-run, at most twice per version.
            if _screen_attempts(entry, latest["path"]) < MAX_SCREEN_ATTEMPTS \
                    and screens_running.get(row["ws"], 0) < MAX_SCREENS_PER_WS:
                # No screen at all: a hardening round whose own pilot failed (2026-10-14, revoked OpenAI key) left an
                # unmeasured revision; pilot it. A screen short of valid episodes is topped up or relaunched.
                run = launch(row["ws"], ["screen", *(["--relaunch-incomplete"] if latest.get("screen_exists") and latest.get("topup_exhausted") and not latest.get("hack_flags") else []),
                                         "--include-unvalidated", "--label", "void-rerun" if latest.get("screen_exists") else "pilot", latest["path"]], dry)
                entry["actions"].append({"kind": "screen", "version": latest["path"], "run": run, "at": time.time()})
                screens_running[row["ws"]] = screens_running.get(row["ws"], 0) + 1
                summary["hardening"].append(f"{name}: re-pilot (voids) {why}")
            else:
                summary["building"].append(f"{name} (screen incomplete {why})")
            continue
        if kind == "hard" and latest.get("hack_flags"):
            # qualify refuses a pilot with unresolved reward-hacking flags; only a human adjudicates them.
            summary["blocked"].append(f"{name}: HARD pilot on {Path(latest['path']).name} {why or ''} held: "
                                      f"{latest.get('hack_flags')} hack flag(s) await human adjudication")
            continue
        if kind == "hard":
            if any(a["kind"] == "release" and a.get("version") == latest["path"] for a in entry["actions"]):
                summary["blocked"].append(f"{name}: release already launched for {latest['path']} and not ready")
                continue
            if releases_running.get(row["ws"], 0) >= MAX_RELEASES_PER_WS:
                summary["hard_validated"].append(f"{name} (waiting for a release slot)")
                continue
            run = launch(row["ws"], ["shipfix", latest["path"], "--release"], dry)
            entry["actions"].append({"kind": "release", "version": latest["path"], "run": run, "at": time.time()})
            releases_running[row["ws"]] = releases_running.get(row["ws"], 0) + 1
            summary["releasing"].append(name)
            continue
        # Hardening measures from the version it is given (harden.baseline reads that version's pilot), so the
        # measured version itself is passed, never the lineage root (c-005, 2026-10-12: refused for lack of a
        # baseline on the root). Two hidden-only rounds, then one full redesign round, then a human looks.
        fast = sum(1 for a in entry["actions"] if a["kind"] == "harden")
        full = sum(1 for a in entry["actions"] if a["kind"] == "harden_full")
        if row.get("harden_attempts", 0) >= EXTENDED_BUDGET or fast + full >= EXTENDED_BUDGET:
            if _release_earlier_hard(row, entry, versions, releases_running, summary, name, dry):
                continue
            summary["not_hard_spent"].append(f"{name}: {why}")
            continue
        same = [a for a in entry["actions"] if a.get("version") == latest["path"] and a["kind"] in ("harden", "harden_full")]
        cleared = [a["at"] for a in entry["actions"] if a["kind"] in ("resummarize", "hack_triage")
                   and a.get("version") == latest["path"]]            # hack-triage --resummarize re-derives too
        if same and cleared and not latest.get("hack_flags") and all(a["at"] < max(cleared) for a in same):
            # The round on this version was refused for flags triage has since ruled benign and the screen was
            # re-derived: the round runs again rather than counting as one that produced nothing.
            same = []
        if same and latest.get("hack_flags"):
            # Results are not re-run to escape a reward-hacking flag: only a human adjudicates it.
            summary["blocked"].append(f"{name}: {latest.get('hack_flags')} hack flag(s) on {Path(latest['path']).name} "
                                      "await human adjudication (hardening refused)")
            continue
        if same and why and all(str(w).startswith("signal:") for w in why) and not any(
                a["kind"] == "signal_fix" and a.get("version") == latest["path"] for a in entry["actions"]):
            # Hard on both models but short of the learning signal (2026-10-18 c-032 v2: 0.0 / 0.0 with 2 independent
            # hard decisions, 3 needed): harden refuses a target-met lineage, so the round produced nothing and the
            # task sat in not_hard_spent. An operator --fix reopens the lineage at this version, once.
            ideas_file = _ideas_file(row, name)
            run = launch(row["ws"], ["harden", latest["path"], "--apply", "--fast",
                                     *(["--traps", str(ideas_file)] if ideas_file.is_file() else []),
                                     *[arg for w in why for arg in ("--fix", "the measured pilot fails the release's learning-signal bar -- "
                                                                    + str(w) + "; add independent determinate decisions "
                                                                    "(different trap types, each failed in 2+ episodes of BOTH models) "
                                                                    "without weakening the decisions that already bite")],
                                     *_budget_args(row)], dry)
            entry["actions"].append({"kind": "signal_fix", "version": latest["path"], "why": why, "run": run,
                                     "at": time.time()})
            summary["hardening"].append(f"{name}: signal fix on {Path(latest['path']).name}: {why}")
            continue
        if same and _budget_args(row) and not any(a.get("extended") for a in entry["actions"]):
            # The round produced nothing because the default budget was spent: the user's 2026-10-13 "repair all the
            # tasks" raises it once, explicitly, to EXTENDED_BUDGET (a full redesign round under the learning-signal brief).
            run = launch(row["ws"], ["harden", latest["path"], "--apply", *_budget_args(row)], dry)
            entry["actions"].append({"kind": "harden_full", "version": latest["path"], "why": why, "run": run,
                                     "at": time.time(), "extended": True})
            summary["hardening"].append(f"{name}: extended-budget round ({row.get('harden_attempts')} spent)")
            continue
        if same:
            # A round that produced no revision is usually harden refusing a pilot that did not measure cleanly
            # (2026-10-13 c-009-tkinter: "2 void episodes leave 3 valid"): re-pilot with a fresh campaign once,
            # then allow one more round on the same version.
            if len(same) < 2 and _screen_attempts(entry, latest["path"]) < 1 \
                    and screens_running.get(row["ws"], 0) < MAX_SCREENS_PER_WS:
                run = launch(row["ws"], ["screen", "--relaunch-incomplete", "--include-unvalidated", "--label",
                                         "void-rerun", latest["path"]], dry)
                entry["actions"].append({"kind": "screen", "version": latest["path"], "run": run, "at": time.time()})
                screens_running[row["ws"]] = screens_running.get(row["ws"], 0) + 1
                summary["hardening"].append(f"{name}: re-pilot before another round on {Path(latest['path']).name}")
                continue
            if len(same) >= 2 or _screen_attempts(entry, latest["path"]) < 1:
                summary["not_hard_spent"].append(f"{name}: a round on {Path(latest['path']).name} produced no new version: {why}")
                continue
        # User 2026-10-18: "harden the 14 easy tasks ... pass@1<=0.5 on both". A version both models still solve
        # (pass@1 >= SATURATED on each) had its rules announced by the instruction (W12: 80% of 56 traps), which a
        # hidden-only round cannot change: after one hidden-only round it goes to a full redesign under THE PROXY
        # DOCTRINE and the binding hardness gate, with the task's operator trap ideas when the batch has them.
        screen = latest.get("screen") or {}
        saturated = bool(screen) and all((r.get("pass_at_1") or 0) >= SATURATED for r in screen.values())
        ideas = _ideas_file(row, name)
        idea_args = ["--traps", str(ideas)] if ideas.is_file() else []
        if _in_batch(row["lineage"]) and saturated and fast >= 1 and full == 0:
            run = launch(row["ws"], ["harden", latest["path"], "--apply", *idea_args, *_budget_args(row)], dry)
            entry["actions"].append({"kind": "harden_full", "version": latest["path"], "why": why, "run": run,
                                     "at": time.time(), "saturated": True, "ideas": bool(idea_args)})
        elif fast < MAX_FAST and full == 0 and row.get("harden_attempts", 0) < 3:
            run = launch(row["ws"], ["harden", latest["path"], "--apply", "--fast", *idea_args], dry)
            entry["actions"].append({"kind": "harden", "version": latest["path"], "why": why, "run": run, "at": time.time()})
        else:
            run = launch(row["ws"], ["harden", latest["path"], "--apply", *idea_args, *_budget_args(row)], dry)
            entry["actions"].append({"kind": "harden_full", "version": latest["path"], "why": why, "run": run, "at": time.time()})
        summary["hardening"].append(f"{name}: {why}")
    if not dry:
        try:
            _write_ledger(rows)
        except Exception as exc:                     # noqa: BLE001 -- a ledger failure must not end the cycle
            log(f"ledger failed: {type(exc).__name__}: {exc}")
    if ready_rows and not dry and PACKAGE:
        package(ready_rows, state)
    if not dry:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1))
    log("SUMMARY " + json.dumps({k: (len(v), v[:40]) for k, v in summary.items()}))
    return summary


def main() -> int:
    loop, dry = "--loop" in sys.argv, "--dry-run" in sys.argv
    interval = int(sys.argv[sys.argv.index("--interval") + 1]) if "--interval" in sys.argv else 600
    while True:
        try:
            cycle(dry)
        except Exception as exc:                     # noqa: BLE001 -- a bad cycle must not end the driver
            log(f"cycle failed: {type(exc).__name__}: {exc}")
        if not loop:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
