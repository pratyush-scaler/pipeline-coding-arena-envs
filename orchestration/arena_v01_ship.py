"""Arena v0.1 delivery: convert a released package, calibrate it on Harbor 0.24, assemble the evidence, and ship
the zip only when Arena's own checker (lmarena/arena-rle-formatcheck-external, pinned) passes it.

    arena_v01_ship.py convert <delivered.zip> --domain D --language L --archive NAME [--batch B]
    arena_v01_ship.py launch  <env-id> <workspace>
    arena_v01_ship.py status  [<env-id>...]
    arena_v01_ship.py finish  <env-id> [--skip-regrade] [--skip-pull]
    arena_v01_ship.py check   <zip>
    arena_v01_ship.py run     <workspace> -- <pipeline.cli args>
    arena_v01_ship.py release-finish <bundle> <workspace> --archive NAME --sessions DIR [--batch B]
                              [--previous-version V --previous-date D] [--skip-regrade] [--skip-pull]

Release qualification (user, 2026-10-24: "use the 0.24 calibration as release qualification"). A final version is
released on its Arena v0.1 package from this checkout's staged code, so its controls and 10 x 2 run on Harbor 0.24
(pipeline/arena_v01_release.py):

    arena_v01_ship.py run <ws> -- shipfix <bundle> --release --arena-v01 '{"domain": D, "prompt_language": L}'
    arena_v01_ship.py release-finish <bundle> <ws> --archive NAME --sessions DIR

`release-finish` builds the supplier and v0.1 packages ON the workspace from the released version (refused unless
the runtime tree is exactly the calibrated one), adds Arena's provenance, and assembles the evidence from the
release's own campaigns (`finish`); nothing is calibrated twice. `convert`/`launch` stay for packages delivered
before (a_f2_sports-data-pipeline), whose rows `finish` reads exactly as before.

The delivery format, receipts and schemas are Arena's (docs/delivery-format.md in the checker). Calibration and
the Oracle evidence must come from Harbor 0.24.0 (Q19/Q21: rerun, never relabelled): this checkout's
requirements-native.lock pins it, and `launch` stages this checkout's code beside the live checkout's runs on the
workspace, so a snapshot with that lock gets its own interpreter (coder_run.NATIVE_VENV) and nothing live moves.

Everything lands at the live checkout's absolute paths, locally and on the workspace (the pipeline's convention):
packages under LIVE/arena-v01-packages/<env-id>, campaigns under LIVE/pipeline-artifacts/arena-v01/<env-id>, zips
under LIVE/arena-v01-zips. LIVE is the main checkout of this repository (ARENA_LIVE_ROOT overrides it).

Arena's provenance must cite the original export archive by a supplier-free name (Q37), and the checker resolves
--source-archive paths, so each cited archive is a real file (a hard link) of that name in LIVE/arena-v01-archives.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _live_root() -> Path:
    if os.environ.get("ARENA_LIVE_ROOT"):
        return Path(os.environ["ARENA_LIVE_ROOT"]).resolve()
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout.strip()
    return Path(common).parent


LIVE = _live_root()
os.environ.setdefault("ARENA_DUMP", str(Path.home() / "Downloads/arena-dumps"))  # config reads it at import

from pipeline import arena_v01 as conv_mod  # noqa: E402
from pipeline import arena_v01_evidence as ev  # noqa: E402
from pipeline import arena_v01_package as ap  # noqa: E402
from pipeline.benchmark import coder as coder_mod, coder_run  # noqa: E402

q = coder_run.q
PACKAGES = LIVE / "arena-v01-packages"
CAMPAIGNS = LIVE / "pipeline-artifacts/arena-v01"
ZIPS = LIVE / "arena-v01-zips"
ARCHIVES = LIVE / "arena-v01-archives"
RECORD = PACKAGES / "ship.json"
HACK_REVIEWS = PACKAGES / "hack-reviews.json"
WAIVERS = ROOT / "orchestration/arena_v01_waivers.json"
KEYS_DIR = "/dev/shm/arena-v01-recal"  # private keys file: the shared one live runs read is never rewritten

#: Arena's checker, pinned to the commit the delivery format was read from (2026-10-24).
FORMATCHECK = Path(os.environ.get("ARENA_FORMATCHECK", LIVE / ".arena-formatcheck"))
FORMATCHECK_COMMIT = "229e570"
FORMATCHECK_URL = "https://github.com/lmarena/arena-rle-formatcheck-external.git"
SUPPLIER = ["--supplier", "scaler", "--supplier", "Scaler AI Labs"]  # Q37, as the task bank's config names them

CALIBRATION = [("oracle", ["--agent", "oracle", "-r", "1"]), ("nop", ["--agent", "nop", "-r", "1"]),
               ("claude-fable-5-1", ["--agent", "harness", "-m", "claude-fable-5-1", "-r", "10", "-j", "2"]),
               ("gpt-6-astra", ["--agent", "harness", "-m", "gpt-6-astra", "-r", "10", "-j", "2"])]
MODELS = {"claude-fable-5-1": {"provider_model": "anthropic/claude-fable-5-1", "harness_log": "claude-code.txt"},
          "gpt-6-astra": {"provider_model": "openai/gpt-6-astra", "harness_log": "codex.txt"}}
SUPPLEMENTAL = {"controls": "verification/controls.json", "probes": "verification/probes.json",
                "coverage": "verification/coverage-critic.json", "hacking": "verification/REWARD-HACKING.md"}

#: Our pipeline checkouts as they appear in our own supplemental reports: written as <pipeline>, since Arena's
#: leak scan rejects host paths and they identify our machine.
PIPELINE_ROOTS = tuple(dict.fromkeys(str(p) for p in (
    LIVE, ROOT, Path.home() / "Downloads/arena-envs-codingtasks-large-dump",
    Path.home() / "Downloads/arena-envs-coding-v2", Path("/home/coder/pipeline"))))
HOST_PATH = re.compile(r"/Users/[A-Za-z0-9_.-]+|/home/(?!(?:agent|user|node|ubuntu|root|app|runner|worker|harbor|e2b)\b)"
                       r"[A-Za-z0-9_.-]+/")

#: Regrade one captured step state twice with the task's own verifier image (receipt.verifier_repeat).
REGRADE = r'''set -euo pipefail
PKG=$1; STEP=$2; ART=$3; OUT=$4
TAG="arena-v01-regrade-$(basename "$PKG" | tr "[:upper:]_" "[:lower:]-")"
docker build -q -t "$TAG" "$PKG/tests" >/dev/null
for n in 1 2; do
  d="$OUT/$n/verifier"; rm -rf "$OUT/$n"; mkdir -p "$d"
  cid=$(docker create --network none -e ARENA_STEP="$STEP" "$TAG" /bin/sh -c '/tests/test.sh')
  docker cp "$ART/app" "$cid:/" >/dev/null
  docker start -a "$cid" > "$d/test-stdout.txt" 2>&1 || echo "verifier exited non-zero (run $n)" >&2
  docker cp "$cid:/logs/verifier/." "$d/" >/dev/null
  docker rm -f "$cid" >/dev/null
done
echo "regraded $STEP twice into $OUT"
'''

#: Per campaign on the workspace: report.json's summary and the run's exit code.
STATUS_PROBE = r'''
import json, os, sys
for line in sys.stdin:
    key, out, rid = line.split()
    rep, ex = os.path.join(out, "report.json"), os.path.join(sys.argv[1], rid, "exit_code")
    s = {}
    try:
        r = json.load(open(rep)); s = r.get("summary") or {}; st = r.get("status")
    except Exception:
        st = "starting"
    done = os.path.exists(ex)
    print(json.dumps({"key": key, "status": st, "exit": open(ex).read().strip() if done else None,
                      "valid": s.get("valid"), "requested": s.get("requested"), "mean": s.get("mean_reward")}))
'''


def _record() -> dict:
    return json.loads(RECORD.read_text()) if RECORD.is_file() else {}


def _save(record: dict) -> None:
    RECORD.parent.mkdir(parents=True, exist_ok=True)
    tmp = RECORD.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=1) + "\n")
    tmp.replace(RECORD)


def _ssh(ws: str, script: str, timeout: int = 3600, **kw):
    return coder_mod.ssh(ws, script, timeout=timeout, **kw)


# ------------------------------------------------------------------------------------------------ convert

def _archive_member(archive: Path, session_id: str, data: bytes) -> str:
    with zipfile.ZipFile(archive) as zf:
        hits = [n for n in zf.namelist() if n.endswith(f"/{session_id}/data.json")]
        if len(hits) != 1:
            raise SystemExit(f"{archive.name}: {len(hits)} members hold {session_id}/data.json")
        if zf.read(hits[0]) != data:  # the cited member holds exactly the session bytes we read
            raise SystemExit(f"{archive.name}:{hits[0]} differs from the session's data.json")
        return hits[0]


def _seed_span(session_dir: Path, seed_index: int) -> tuple[int, int]:
    """The seed's message range, from the pipeline's own seed builder (it reproduced 50/50 recorded spans)."""
    from pipeline.dump import recover, signals
    sid, sessions = session_dir.name, session_dir.parent
    seed = signals.build_seeds(recover.load_trace(sid, sessions), signals.load_outcomes(sid, sessions))[seed_index]
    return seed.lo, seed.hi


def convert(zip_path: Path, *, domain: str, language: str, archive: str, sessions: Path, batch: str | None) -> dict:
    cited = ARCHIVES / archive
    if not cited.is_file():
        raise SystemExit(f"{cited} is missing: hard-link the original export there under its supplier-free name")
    made = conv_mod.convert_runtime(zip_path, PACKAGES, domain=domain, prompt_language=language)
    env, pkg, src = made["env_id"], Path(made["package"]), Path(made["source_package"])
    ledger = _provenance(pkg, src, cited, archive, sessions)
    record = _record()
    record[env] = {**made, "source_zip": str(zip_path.resolve()), "domain": domain, "prompt_language": language,
                   "batch": batch, "archive": archive, "delivered_on": date.fromtimestamp(zip_path.stat().st_mtime)
                   .isoformat(), "converted_at": time.time()}
    _save(record)
    origins: dict[str, int] = {}
    for e in ledger["entries"]:
        origins[e["origin"]] = origins.get(e["origin"], 0) + 1
    print(f"{env}: {made['from_version']} -> {made['version']}; ledger {origins}")
    return record[env]


def _provenance(pkg: Path, src: Path, cited: Path, archive: str, sessions: Path) -> dict:
    """Arena's provenance (source-scope.json, ledger.json) for a converted package; returns the ledger."""
    spec = json.loads((src / "authoring/spec.json").read_text())
    sid, seed = spec["source"]["session_id"], int(spec["source"]["seed_index"])
    session_dir = sessions / sid
    member = _archive_member(cited, sid, (session_dir / "data.json").read_bytes())
    scope, ledger = conv_mod.provenance(pkg, src, session_dir=session_dir, archive=archive, member=member,
                                        span=_seed_span(session_dir, seed), intent=spec.get("intent", ""))
    (pkg / "provenance").mkdir(exist_ok=True)
    for name, doc in (("source-scope.json", scope), ("ledger.json", ledger)):
        (pkg / "provenance" / name).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    return ledger


# ------------------------------------------------------------------------------------------------ launch

def stage(ws: str) -> tuple[Path, str]:
    """This checkout's code as LIVE/.staged/<sha> on the workspace, with its own Harbor interpreter when its
    native lock differs from the live checkout's (coder_run.stage_code, with the live checkout as the layout)."""
    sha = coder_mod._tree_sha(coder_run.CODE_PATHS)
    dest = LIVE / coder_run.STAGED / sha[:16]
    filters = ["- __pycache__/", "- *.pyc", "- keys.env*", "- workspace.toml"]
    _ssh(ws, f"mkdir -p {q(dest)}", timeout=120)
    for rel in coder_run.CODE_PATHS:
        local = ROOT / rel
        if not local.exists():
            continue
        if local.is_dir():
            coder_run.rsync(ws, f"{local}/", f"{dest / rel}/", push=True, delete=True, filters=filters, timeout=1800)
        else:
            coder_run.rsync(ws, str(local), str(dest / rel), push=True, timeout=600)
    _ssh(ws, 'export PATH="$HOME/.local/bin:$PATH" && '
             f'python3 -c {q(coder_run.NATIVE_VENV)} {q(LIVE)} {q(dest)} "$(command -v uv)"', timeout=1800)
    names = ",".join(Path(rel).parts[0] for rel in coder_run.CODE_PATHS)
    _ssh(ws, f"python3 -c {q(coder_run.LINK_FARM)} {q(LIVE)} {q(dest)} {q(names)}", timeout=120)
    _ssh(ws, f"echo {q(sha)} > {q(dest / '.arena-code-sha')}", timeout=120)
    return dest, sha


def _start(ws: str, dest: Path, sha: str, args: list[str]) -> str:
    rid = coder_run.run_id()
    request = {"root": str(dest), "run_dir": str(LIVE / coder_run.RUNS / rid),
               "argv": [str(LIVE / ".venv/bin/python"), "-m", "pipeline.cli", *args],
               # a batch with its own artifacts root (pipeline-artifacts-bash100) keeps it on the workspace
               "env": coder_run.remote_env({"ARENA_PIPELINE_ARTIFACTS": os.environ.get(
                   "ARENA_PIPELINE_ARTIFACTS", str(LIVE / "pipeline-artifacts")),
                   # every episode of a 10 x 2 calibration at once (capacity.NATIVE_CAPACITY defaults to 16)
                   **{k: os.environ[k] for k in ("ARENA_NATIVE_CONCURRENCY", "ARENA_V01_RELEASE_WITHOUT_SCREEN", "ARENA_REVIEW_MODEL")
                      if os.environ.get(k)}}),
               "keys": coder_run.keys_values(LIVE / "keys.env"), "code_sha": sha, "keys_dir": KEYS_DIR}
    _ssh(ws, f"mkdir -p -m 700 {q(KEYS_DIR)}", timeout=120)
    coder_mod.ssh(ws, coder_mod._with_docker(f"python3 -c {q(coder_run.LAUNCHER)}"),
                  stdin=json.dumps(request).encode(), timeout=300)
    return rid


def launch(env: str, ws: str) -> dict:
    record = _record()
    row = record[env]
    pkg = Path(row["package"])
    target = PACKAGES / env
    _ssh(ws, f"mkdir -p {q(target)}", timeout=120)
    coder_run.rsync(ws, f"{pkg}/", f"{target}/", push=True, delete=True, timeout=1800, compress=True,
                    filters=["- __pycache__/"])
    dest, sha = stage(ws)
    launches = {}
    for label, args in CALIBRATION:
        out = CAMPAIGNS / env / f"cal-{label}"
        launches[label] = {"run": _start(ws, dest, sha, ["benchmark", str(target), *args, "--legacy-diagnostic",
                                                         "-o", str(out)]), "out": str(out)}
        time.sleep(1)
    row.update(workspace=ws, code_sha=sha, launches=launches, launched_at=time.time())
    _save(record)
    print(json.dumps({env: {k: v["run"] for k, v in launches.items()}}, indent=1))
    return row


def status(envs: list[str]) -> dict:
    record = _record()
    rows = {}
    by_ws: dict[str, list] = {}
    for env in envs or [e for e, r in record.items() if r.get("launches") and not r.get("accepted")]:
        for label, l in record[env].get("launches", {}).items():
            by_ws.setdefault(record[env]["workspace"], []).append((f"{env}/{label}", l))
    for ws, items in by_ws.items():
        stdin = "\n".join(f"{k} {l['out']} {l['run']}" for k, l in items).encode()
        proc = coder_mod.ssh(ws, f"python3 -c {q(STATUS_PROBE)} {q(str(LIVE / coder_run.RUNS))}", stdin=stdin,
                             timeout=600, check=False)
        for line in coder_mod._clean(proc.stdout.decode()).splitlines():
            if line.startswith("{"):
                d = json.loads(line)
                rows[d["key"]] = d
    out = {}
    for key, d in sorted(rows.items()):
        env, label = key.split("/")
        out.setdefault(env, {})[label] = d
    for env, camps in out.items():
        cells = [f"{c[:6]}:{'done' if d['exit'] is not None else 'run'} {d['valid']}/{d['requested']} "
                 f"m={None if d['mean'] is None else round(d['mean'], 2)}" for c, d in camps.items()]
        ready = all(d["exit"] is not None for d in camps.values())
        print(f"{env:40s} {' | '.join(cells)}{'  READY' if ready else ''}")
    return out


# ------------------------------------------------------------------------------------------------ finish

def scrub_report(text: str, env: str, where: str) -> str:
    text = re.sub(r"\bc-\d{3}-[a-z0-9][a-z0-9-]*", env, text)  # internal working name -> delivered id
    for root in PIPELINE_ROOTS:
        text = text.replace(root, "<pipeline>")
    left = sorted(set(HOST_PATH.findall(text)))
    if left:
        raise SystemExit(f"{where}: host path(s) left in a supplemental report: {left[:3]}")
    return text


def _measured(campaign: Path) -> dict:
    report = json.loads((campaign / "report.json").read_text())
    trials = []
    for t in report.get("trials") or []:
        folder = campaign / "jobs/run" / t["id"]
        result = json.loads((folder / "result.json").read_text()) if (folder / "result.json").is_file() else {}
        by = {sr.get("step_name"): sr for sr in result.get("step_results") or []}
        trials.append({"id": t["id"], "valid": t.get("valid"), "result_file": str(folder / "result.json"),
                       "steps": [{"name": s["name"], "evidence_dir": str(folder / "steps" / s["name"]),
                                  "agent_result": (by.get(s["name"]) or {}).get("agent_result")}
                                 for s in t.get("steps") or []]})
    return {"trials": trials}


def _layout(env: str, row: dict) -> dict:
    """Where `finish` reads a package's campaigns: its own launches (CAMPAIGNS/<env>/cal-*), or -- for a package a
    pipeline release qualified (row["release"]) -- that release's campaigns of record, by their recorded paths."""
    remote = CAMPAIGNS / env
    rel = row.get("release")
    if not rel:
        return {"oracle": remote / "cal-oracle", "nop": remote / "cal-nop",
                "models": {m: remote / f"cal-{m}" for m in MODELS}, "bundle": None}
    from pipeline.benchmark.harbor import resolve_model
    cal = rel["calibration"]
    models = {}
    for m in MODELS:
        parts = cal["campaigns"].get(resolve_model(m)) or []
        if len(parts) != 1:
            raise SystemExit(f"{env}: {m} has {len(parts)} campaign part(s) in the release; the v0.1 assembler "
                             "reads exactly one campaign per model (void top-ups are not assembled yet)")
        models[m] = Path(parts[0])
    return {"oracle": Path(cal["oracle"]), "nop": Path(cal["nop"]), "models": models, "bundle": Path(rel["bundle"])}


def _hack_scan(pkg: Path, env: str, campaigns: dict, bundle: Path | None = None) -> tuple[dict, int, int, int]:
    """A released version is scanned as its release scanned it -- with the bundle, whose human adjudications
    (verification/hack-adjudications.json) clear the flags they rule on; HACK_REVIEWS applies either way."""
    from pipeline.attribution import trajectory_hacks
    findings = {m: trajectory_hacks(bundle or pkg, _measured(campaigns[m])) for m in MODELS}
    reviews = json.loads(HACK_REVIEWS.read_text())["reviews"] if HACK_REVIEWS.is_file() else []
    for m, fs in findings.items():
        for f in fs:
            r = next((r for r in reviews if r["env"] == env and r["model"] in ("*", m)
                      and r["trial"] in ("*", f.get("trial")) and r["kind"] == f["kind"]), None)
            if r:
                f["review"] = {"verdict": r["verdict"], "reason": r["reason"]}
    flagged = sum(1 for fs in findings.values() for f in fs
                  if f["kind"] != "uninspectable" and (f.get("review") or {}).get("verdict") != "benign")
    blind = sum(1 for fs in findings.values() for f in fs if f["kind"] == "uninspectable")
    benign = sum(1 for fs in findings.values() for f in fs if (f.get("review") or {}).get("verdict") == "benign")
    return findings, flagged, blind, benign


def finish(env: str, *, regrade: bool = True, pull: bool = True) -> int:
    record = _record()
    row = record[env]
    ws = row["workspace"]
    ev.load_secrets(LIVE / "keys.env")  # harness credentials an agent may have printed; redacted from copied logs
    remote = CAMPAIGNS / env
    lay = _layout(env, row)
    pkg, src = Path(row["package"]), Path(row["source_package"])
    steps = [s["name"] for s in tomllib.loads((pkg / "task.toml").read_text())["steps"]]
    if regrade:
        trial = _ssh(ws, f"ls -d {q(str(lay['oracle'] / 'jobs/run'))}/*/ | head -1").stdout.decode() \
            .strip().splitlines()[-1].rstrip("/")
        _ssh(ws, f"mkdir -p {q(str(remote))} && cat > {q(str(remote / 'regrade.sh'))}", stdin=REGRADE.encode(),
             timeout=120)
        for s in steps:
            out = _ssh(ws, coder_mod._with_docker(
                f"bash {q(str(remote / 'regrade.sh'))} {q(str(PACKAGES / env))} {q(s)} "
                f"{q(f'{trial}/steps/{s}/artifacts')} {q(str(remote / 'repeats' / s))}"))
            print(coder_mod._clean(out.stdout.decode())[-300:])
    if pull:
        remote.mkdir(parents=True, exist_ok=True)
        coder_run.rsync(ws, f"{remote}/", f"{remote}/", push=False, timeout=7200, compress=True,
                        filters=["- package/", "- */jobs/run/*/artifacts/logs/"])
    tree_before = conv_mod.task_tree_sha256(pkg)
    for d in ("calibration", "verification", "authoring"):
        shutil.rmtree(pkg / d, ignore_errors=True)
    have = {}
    for k, rel in SUPPLEMENTAL.items():  # the source package's own reports, bytes unchanged but for scrubbing
        if (src / rel).is_file():
            (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
            (pkg / rel).write_text(scrub_report((src / rel).read_text(encoding="utf-8"), env, rel), encoding="utf-8")
            have[k] = rel
    probe = _ssh(ws, coder_mod._with_docker("docker version --format '{{.Server.Version}}'; "
                                            ". /etc/os-release; echo \"$NAME $VERSION_ID\"; uname -m"), timeout=300)
    lines = [l.strip() for l in coder_mod._clean(probe.stdout.decode()).splitlines() if l.strip()]
    docker_version, host_os, machine = lines[-3], lines[-2], lines[-1]
    if machine != "x86_64":
        raise SystemExit(f"{ws}: {machine} is not linux/amd64")
    models = {m: {"campaign": lay["models"][m], "provider_model": v["provider_model"],
                  "harness_log": v["harness_log"], "effort": "high", "requested": 10} for m, v in MODELS.items()}
    history = _history(row)
    summary = ap.assemble(pkg, src, oracle=lay["oracle"], nop=lay["nop"], models=models,
                          repeats=remote / "repeats", history=history,
                          runtime={"version": docker_version, "host_os": host_os})
    summary["emails_redacted"] = ev.redact_log_addresses(pkg)
    waivers = json.loads(WAIVERS.read_text()).get(env, {}) if WAIVERS.is_file() else {}
    for note in waivers.values():  # supplier-approved waivers: stated in the README, never hidden
        summary.setdefault("notes", []).append(note)
    ev.write_json(pkg / "authoring/traps.json", ap.traps(pkg, src))
    differ = ap.grader_unchanged(pkg, src)
    if differ:
        raise SystemExit(f"{env}: grader files changed since the controls ran: {differ}")
    findings, n_flag, n_blind, n_benign = _hack_scan(pkg, env, lay["models"], lay["bundle"])
    ev.write_json(pkg / "verification/hack-scan.json",
                  {"schema": "arena-pipeline/hack-scan/1", "scanner": "pattern scan of every agent command in the "
                   "calibration trajectories (test access, graded writes, symlinks, tamper, undeclared outputs)",
                   "findings": findings})
    qa = ap.quality_assessment(pkg, evidence_files=have, assessor_version="arena-v01/1")
    sec = next(c for c in qa["checks"] if c["id"] == "grading_security")
    sec["evidence"].append({"path": "verification/hack-scan.json"})
    if n_flag or n_blind:
        sec["status"] = "not_evaluated"
        sec["rationale"] += (f" The calibration trajectory scan raised {n_flag} finding(s) and {n_blind} step(s) it "
                             "could not inspect; they await review, so this dimension is not yet claimed.")
    else:
        sec["rationale"] += (" The calibration trajectory scan read every graded run" + (
            f"; its {n_benign} pattern match(es) were each reviewed and found benign (verification/hack-scan.json "
            "gives each one's reason)." if n_benign else " and found no pattern match."))
    qa["status"] = "fail" if any(c["status"] == "fail" for c in qa["checks"]) else \
        "not_evaluated" if any(c["status"] == "not_evaluated" for c in qa["checks"]) else "pass"
    ev.write_json(pkg / "verification/quality-assessment.json", qa)
    (pkg / "README.md").write_text(ap.readme(pkg, summary=summary, history=history))
    ev.write_json(pkg / "PACKAGE-MANIFEST.json", ev.package_manifest(pkg, env_id=env, version=row["version"]))
    if not (conv_mod.task_tree_sha256(pkg) == tree_before == summary["tree"]):
        raise SystemExit(f"{env}: the task tree changed while the evidence was assembled")
    zpath = _zip(pkg, env, row["version"])
    failed = check(zpath, waived=set(waivers))
    row.update(zip=str(zpath), finished_at=time.time(), hack_flags=n_flag, uninspectable=n_blind,
               oracle=summary.get("oracle"), nop=summary.get("nop"), models=summary.get("models"),
               format_failures=failed, accepted=not failed and not n_flag and not n_blind)
    _save(record)
    print(json.dumps({k: row.get(k) for k in ("zip", "oracle", "nop", "models", "hack_flags", "uninspectable",
                                              "format_failures", "accepted")}, indent=1, default=str))
    return 0 if row["accepted"] else 1


def _history(row: dict) -> list[dict]:
    if not row.get("release"):
        return [{"version": row["from_version"], "date": row["delivered_on"], "class": "minor",
                 "summary": "Previous delivery (supplier layout); calibrated on Harbor 0.22.0.", "calibration": "stale"},
                {"version": row["version"], "date": date.today().isoformat(), "class": "minor",
                 "summary": "Arena v0.1 package format: delivery metadata in [metadata], verifier resources declared, "
                            "rubric per step, reward-details in Arena's schema; recalibrated on Harbor 0.24.0.",
                 "calibration": "current"}]
    previous = [{"version": row["from_version"], "date": row["delivered_on"], "class": "minor",
                 "summary": "Previous delivery (supplier layout).", "calibration": "stale"}] \
        if row.get("from_version") else []
    return previous + [{"version": row["version"], "date": date.today().isoformat(), "class": "minor",
                        "summary": "Arena v0.1 package; its release qualification is this calibration: native Oracle "
                                   "and nop controls and 10 episodes per model on Harbor 0.24.0, on this exact tree.",
                        "calibration": "current"}]


def _zip(pkg: Path, env: str, version: str) -> Path:
    ZIPS.mkdir(parents=True, exist_ok=True)
    zpath = ZIPS / f"{env}@{version}.zip"
    zpath.unlink(missing_ok=True)
    stamp = date.today().timetuple()[:3] + (0, 0, 0)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(p for p in pkg.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(f"{env}/{f.relative_to(pkg).as_posix()}", date_time=stamp)
            info.external_attr = (0o100755 if f.stat().st_mode & 0o111 else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, f.read_bytes())
    return zpath


# ------------------------------------------------------------------------------------------------ release

def run(ws: str, args: list[str]) -> str:
    """Any pipeline.cli command from this checkout's staged code (its Harbor pin) on `ws`, detached; pass task trees
    as absolute paths. `./arena coder runs` / `watch` follow it like any staged run."""
    if not args:
        raise SystemExit("nothing to run: give the pipeline.cli arguments after --")
    dest, sha = stage(ws)
    rid = _start(ws, dest, sha, list(args))
    print(json.dumps({"workspace": ws, "run": rid, "code_sha": sha, "run_dir": str(LIVE / coder_run.RUNS / rid),
                      "argv": list(args)}))
    return rid


#: Bundle folders release-finish never pulls: work areas, not receipts (pipeline.deliver.RECEIPTS is an allowlist).
BUNDLE_PULL_FILTERS = ["- verification/controls-work/", "- verification/local-workers/",
                       "- verification/control-cache/", "- verification/design-sessions/", "- verification/reviews/",
                       "- verification/screens/", "- verification/determinacy/", "- __pycache__/"]


def release_finish(bundle: Path, ws: str, *, archive: str, sessions: Path, batch: str | None = None,
                   previous: tuple[str, str] | None = None, regrade: bool = True, pull: bool = True) -> int:
    bundle = bundle.resolve()
    cited = ARCHIVES / archive
    if not cited.is_file():
        raise SystemExit(f"{cited} is missing: hard-link the original export there under its supplier-free name")
    dest, sha = stage(ws)
    env_vars = " ".join(f"{k}={q(v)}" for k, v in coder_run.remote_env({}).items())
    out = _ssh(ws, f"cd {q(dest)} && env {env_vars} PYTHONPATH={q(dest)} {q(str(LIVE / '.venv/bin/python'))} "
                   f"-m pipeline.arena_v01_release package {q(str(bundle))} --packages {q(str(PACKAGES))}",
               timeout=3600)
    lines = [l for l in coder_mod._clean(out.stdout.decode()).splitlines() if l.startswith("{")]
    if not lines:
        raise SystemExit(f"{bundle}: the workspace packaging printed no record")
    made = json.loads(lines[-1])
    env, version = made["env_id"], made["version"]
    cal = made["calibration"]
    if pull:
        coder_run.rsync(ws, f"{bundle}/", f"{bundle}/", push=False, timeout=3600, compress=True,
                        filters=BUNDLE_PULL_FILTERS)
        for path in (made["package"], made["source_package"]):
            Path(path).mkdir(parents=True, exist_ok=True)
            coder_run.rsync(ws, f"{path}/", f"{path}/", push=False, delete=True, timeout=1800, compress=True)
        parts = [cal["oracle"], cal["nop"], *(p for ps in cal["campaigns"].values() for p in ps)]
        for path in parts:
            Path(path).mkdir(parents=True, exist_ok=True)
            coder_run.rsync(ws, f"{path}/", f"{path}/", push=False, timeout=7200, compress=True,
                            filters=["- package/", "- jobs/run/*/artifacts/logs/"])
    pkg, src = Path(made["package"]), Path(made["source_package"])
    if conv_mod.task_tree_sha256(pkg) != (cal.get("package") or {}).get("task_tree_sha256"):
        raise SystemExit(f"{env}: the pulled package is not the calibrated tree")
    ledger = _provenance(pkg, src, cited, archive, sessions)
    params = made["params"]
    record = _record()
    if env in record and not record[env].get("release"):
        raise SystemExit(f"{env} is already a converted delivery in {RECORD}; it is not replaced by a release row")
    record[env] = {"env_id": env, "from_version": previous[0] if previous else None, "version": version,
                   "package": str(pkg), "source_package": str(src), "task_tree_sha256": made["task_tree_sha256"],
                   "source_zip": None, "domain": params["domain"], "prompt_language": params["prompt_language"],
                   "batch": batch, "archive": archive, "delivered_on": previous[1] if previous else None,
                   "converted_at": time.time(), "workspace": ws, "code_sha": sha,
                   "release": {"bundle": str(bundle), "record": str(bundle / "verification/shipfix-release.json"),
                               "calibration": cal}}
    _save(record)
    origins: dict[str, int] = {}
    for e in ledger["entries"]:
        origins[e["origin"]] = origins.get(e["origin"], 0) + 1
    print(f"{env}@{version}: released {bundle.name} on {cal['format']} (Harbor {', '.join(cal['harbor'])}); "
          f"ledger {origins}")
    # finish's Oracle repeats build the verifier from PACKAGES/<env> on the workspace: the packaging step above wrote it
    return finish(env, regrade=regrade, pull=pull)


# ------------------------------------------------------------------------------------------------ check

def _formatcheck() -> Path:
    if not (FORMATCHECK / "pyproject.toml").is_file():
        subprocess.run(["git", "clone", "-q", FORMATCHECK_URL, str(FORMATCHECK)], check=True)
        subprocess.run(["git", "checkout", "-q", FORMATCHECK_COMMIT], cwd=FORMATCHECK, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=FORMATCHECK, capture_output=True, text=True).stdout
    if not head.startswith(FORMATCHECK_COMMIT):
        raise SystemExit(f"{FORMATCHECK} is at {head[:12]}, not the pinned {FORMATCHECK_COMMIT}")
    return FORMATCHECK


def _checker_interpreter() -> None:
    """`finish` redacts with Arena's own leak patterns (arena_v01_evidence imports the checker), so it runs on the
    pinned checker's interpreter; the pipeline's venv stays as the harness identity hashes it."""
    try:
        import arena_rle_formatcheck  # noqa: F401
    except ImportError:
        fc = _formatcheck()
        subprocess.run(["uv", "sync", "--frozen", "-q"], cwd=fc, check=True)
        py = fc / ".venv/bin/python"
        os.execv(py, [str(py), str(Path(__file__).resolve()), *sys.argv[1:]])


def failures(report: list[dict], waived: set[str] = frozenset()) -> list[str]:
    """Check ids the checker FAILed, less the supplier-approved waivers for this package."""
    return sorted({r["id"] for subject in report for r in subject.get("rows", [])
                   if r.get("status") == "FAIL" and r["id"] not in waived})


def check(zpath: Path, waived: set[str] = frozenset()) -> list[str]:
    fc = _formatcheck()
    out = ZIPS / "checks" / (zpath.stem + ".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["uv", "run", "--frozen", "arena-rle-formatcheck", str(zpath.resolve()), "--json", str(out), *SUPPLIER]
    for a in sorted(ARCHIVES.glob("*.zip")):
        args += ["--source-archive", str(a)]
    proc = subprocess.run(args, cwd=fc, capture_output=True, text=True)
    print("\n".join(l for l in proc.stdout.splitlines() if not l.startswith("  [PASS]"))[-4000:])
    failed = failures(json.loads(out.read_text()), waived)
    print(f"{zpath.name}: {'PASS' if not failed else 'FAIL ' + ','.join(failed)}"
          + (f" (waived: {','.join(sorted(waived))})" if waived else ""))
    return failed


def main() -> int:
    from pipeline.cli import load_keys
    ap_ = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap_.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert")
    c.add_argument("zip", type=Path)
    c.add_argument("--domain", required=True)
    c.add_argument("--language", required=True, help="the instruction's language (BCP 47)")
    c.add_argument("--archive", required=True, help="the cited export archive's supplier-free name")
    c.add_argument("--sessions", type=Path, required=True, help="the unpacked dump's sessions directory")
    c.add_argument("--batch")
    lch = sub.add_parser("launch")
    lch.add_argument("env")
    lch.add_argument("workspace")
    st = sub.add_parser("status")
    st.add_argument("envs", nargs="*")
    f = sub.add_parser("finish")
    f.add_argument("env")
    f.add_argument("--skip-regrade", action="store_true")
    f.add_argument("--skip-pull", action="store_true")
    ck = sub.add_parser("check")
    ck.add_argument("zip", type=Path)
    rn = sub.add_parser("run", help="a pipeline.cli command from this checkout's staged code (Harbor 0.24)")
    rn.add_argument("workspace")
    rn.add_argument("args", nargs=argparse.REMAINDER)
    rf = sub.add_parser("release-finish", help="the v0.1 zip of a version `shipfix --release --arena-v01` made ready")
    rf.add_argument("bundle", type=Path)
    rf.add_argument("workspace")
    rf.add_argument("--archive", required=True, help="the cited export archive's supplier-free name")
    rf.add_argument("--sessions", type=Path, required=True, help="the unpacked dump's sessions directory")
    rf.add_argument("--batch")
    rf.add_argument("--previous-version", help="the version this package replaces, when it was delivered before")
    rf.add_argument("--previous-date", help="that delivery's date (YYYY-MM-DD)")
    rf.add_argument("--skip-regrade", action="store_true")
    rf.add_argument("--skip-pull", action="store_true")
    a = ap_.parse_args()
    if a.cmd in ("finish", "release-finish"):
        _checker_interpreter()
    load_keys(prompt_if_missing=False)
    if a.cmd == "convert":
        convert(a.zip, domain=a.domain, language=a.language, archive=a.archive, sessions=a.sessions, batch=a.batch)
    elif a.cmd == "launch":
        launch(a.env, a.workspace)
    elif a.cmd == "status":
        status(a.envs)
    elif a.cmd == "finish":
        return finish(a.env, regrade=not a.skip_regrade, pull=not a.skip_pull)
    elif a.cmd == "check":
        return 1 if check(a.zip) else 0
    elif a.cmd == "run":
        run(a.workspace, a.args[1:] if a.args[:1] == ["--"] else a.args)
    elif a.cmd == "release-finish":
        if bool(a.previous_version) != bool(a.previous_date):
            ap_.error("--previous-version and --previous-date go together")
        return release_finish(a.bundle, a.workspace, archive=a.archive, sessions=a.sessions, batch=a.batch,
                              previous=(a.previous_version, a.previous_date) if a.previous_version else None,
                              regrade=not a.skip_regrade, pull=not a.skip_pull)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
