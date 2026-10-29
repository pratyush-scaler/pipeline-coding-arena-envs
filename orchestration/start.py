"""./start -- one command from a fresh clone to a running batch of hard coding tasks, and back to it.

  ./start --dump /path/to/dump --workspaces ws1,ws2,... [--batch NAME] [--seeds 40]   first run
  ./start                                                                             resume / keep running
  ./start --check      prerequisites only (nothing launched, no model calls)
  ./start --status     what the batch is doing
  ./start --stop       stop the local loop (runs already on Coder finish on their own)

Every step is idempotent, so re-running resumes where it stopped:
  1 settings      flags are saved to orchestration/config.json (per person, gitignored)
  2 prerequisites keys.env, the dump, workspace.toml's [coder] section, the coder CLI, the workspaces
  3 seeds         `arena select` classifies the dump's sessions; the survivors are frozen as the batch's wave
  4 workspaces    each workspace is bootstrapped once (orchestration/prepare.py)
  5 builds        the wave's ranks are launched across the workspaces (orchestration/launch.py)
  6 the loop      the supervised driver takes every task from build to shipped zip (orchestration/driver.py),
                  and the keepalive keeps the workspaces up; zips land in ship-zips/
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestration import config  # noqa: E402

PY = str(ROOT / ".venv/bin/python")
#: Process patterns anchored on the interpreter, so a shell that merely MENTIONS a script (a `tail -f`, a waiting
#: `until pgrep ...` loop) is never taken for it; 2026-10-18 a waiting shell kept the supervisor from restarting the
#: driver for 23 minutes.
DRIVER = "[Pp]ython[0-9.]* -u orchestration/drive[r].py --loop"
SUPERVISOR = "bash orchestration/supervis[e].sh"
KEEPALIVE = "bash orchestration/keepaliv[e].sh"
ARTIFACTS = ROOT / "pipeline-artifacts"


def say(ok: bool | None, what: str, fix: str = "") -> bool:
    mark = {True: "ok  ", False: "FAIL", None: "--  "}[ok]
    print(f"  [{mark}] {what}" + (f"\n         -> {fix}" if fix and ok is False else ""), flush=True)
    return bool(ok) or ok is None


def env_for(cfg: dict, tree: str | None = None) -> dict:
    """The environment for a step of `tree`'s batch: ITS dump when it names one (2026-10-30 audit: a second batch on
    another dump was classified from the global dump, and launch then found none of its sessions)."""
    env = dict(os.environ)
    dump = ((cfg.get("batches") or {}).get(tree) or {}).get("dump") if tree else None
    if dump or cfg.get("dump"):
        env["ARENA_DUMP"] = dump or cfg["dump"]
    return env


# --------------------------------------------------------------------------- 2 prerequisites

def check(cfg: dict) -> bool:
    print("prerequisites")
    good = True
    try:
        from pipeline.cli import load_keys
        values = load_keys(prompt_if_missing=False)
        anthropic = bool(values.get("ANTHROPIC_AUTH_TOKEN") or values.get("ANTHROPIC_API_KEY"))
        good &= say(anthropic, "keys.env holds an Anthropic credential", "run ./arena keys")
        good &= say(bool(values.get("OPENAI_API_KEY")), "keys.env holds an OpenAI key (Astra calibration, review)",
                    "run ./arena keys")
    except SystemExit as exc:
        good &= say(False, "keys.env", f"run ./arena keys ({exc})")
    dump = Path(cfg.get("dump") or "")
    good &= say(bool(cfg.get("dump")) and (dump / "sessions").is_dir(), f"trace dump {cfg.get('dump') or '(not set)'}",
                "pass --dump /absolute/path/to/dump (a folder with sessions/)")
    toml = ROOT / "workspace.toml"
    if not toml.is_file():
        text = (ROOT / "workspace.example.toml").read_text()
        if cfg.get("dump"):
            text = text.replace('dump = "/absolute/path/to/arena-dump"', f'dump = "{cfg["dump"]}"')
        toml.write_text(text)
    try:
        section = tomllib.loads(toml.read_text()).get("coder") or {}
    except tomllib.TOMLDecodeError as exc:
        section = {}
        print(f"         workspace.toml does not parse: {exc}")
    filled = bool(section.get("url")) and "example.invalid" not in str(section.get("url")) \
        and section.get("template") not in (None, "", "your-template-name")
    good &= say(filled, "workspace.toml [coder] names your Coder deployment and template",
                f"edit {toml}: set [coder] url and template (see the comments in it)")
    coder = shutil.which("coder")
    good &= say(bool(coder), "coder CLI on PATH", "install it from your Coder deployment and run `coder login <url>`")
    names = config.workspaces(cfg)
    good &= say(bool(names), f"workspaces configured: {names}", "pass --workspaces ws1,ws2,...")
    if coder and names:
        out = subprocess.run([coder, "list", "--output", "json"], capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            good &= say(False, "coder list", "run `coder login <url>`")
        else:
            status = {w.get("name"): (w.get("latest_build") or {}).get("status") for w in json.loads(out.stdout or "[]")}
            for name in names:
                if name not in status:
                    good &= say(False, f"workspace {name} exists", f"create it in Coder (e.g. from your template) or drop it")
                elif status[name] != "running":
                    print(f"         starting {name} ({status[name]})", flush=True)
                    subprocess.run([coder, "start", name, "--yes"], capture_output=True, text=True, timeout=1200)
                    good &= say(True, f"workspace {name} started")
                else:
                    say(True, f"workspace {name} running")
    return good


# --------------------------------------------------------------------------- 3 seeds

def seeds(cfg: dict, tree: str) -> bool:
    batch = cfg["batches"][tree]
    wave = config.WAVES / batch["wave"]
    if wave.is_file():
        rows = json.loads(wave.read_text())["rows"]
        return say(True, f"wave {wave.name}: {sum(1 for r in rows if r['classification'].get('survives'))} surviving seeds")
    used = set()
    for other, b in cfg["batches"].items():
        path = config.WAVES / b.get("wave", "")
        if other != tree and path.is_file():
            used |= {r["seed_key"] for r in json.loads(path.read_text())["rows"] if r["classification"].get("survives")}
    need = int(batch.get("seeds") or 40) + len(used)
    print(f"  classifying the dump until {need} seeds survive (model calls; cached across runs)", flush=True)
    env = env_for(cfg, tree)
    code = subprocess.run(["./arena", "select", "--need", str(need)], cwd=ROOT, env=env).returncode
    classified = ARTIFACTS / "classified.json"
    if code != 0 or not classified.is_file():
        return say(False, "seed selection", "see the output above; re-run ./start to resume")
    doc = json.loads(classified.read_text())
    rows = [r for r in doc["rows"] if r.get("seed_key") not in used and not r["classification"].get("error")]
    if batch.get("track") == "bash_rle":
        # A bash RLE batch: the surviving seeds richest in failed shell decisions, stamped for the track
        # (pipeline/select/bash_rle.py), so the designer leads with bash_rle.MANDATE.
        from pipeline import policy
        from pipeline.select import bash_rle
        picked, skipped = bash_rle.compose(rows, Path(env["ARENA_DUMP"]) / "sessions",
                                           int(batch.get("min_failed") or policy.BASH_RLE_MIN_FAILED_SHELL_CALLS))
        print(f"  bash RLE track: {len(picked)} seed(s) with enough failed shell decisions, {len(skipped)} skipped",
              flush=True)
        rows = picked
    config.WAVES.mkdir(parents=True, exist_ok=True)
    wave.write_text(json.dumps({"schema": "arena-pipeline/classified/wave", "model": doc.get("model"), "rows": rows}))
    survivors = sum(1 for r in rows if r["classification"].get("survives"))
    batch["ranks"] = batch.get("ranks") or f"1-{min(survivors, int(batch.get('seeds') or 40))}"
    config.save(cfg)
    return say(survivors > 0, f"wave {wave.name}: {survivors} surviving seeds (ranks {batch['ranks']})",
               "no seed survived classification: try another dump or a larger --seeds")


# --------------------------------------------------------------------------- 4-6

def prepared(cfg: dict) -> bool:
    todo = [w for w in config.workspaces(cfg) if w not in (cfg.get("prepared") or [])]
    if not todo:
        return say(True, "workspaces bootstrapped")
    print(f"  bootstrapping {todo} (Docker, Harbor, venv, code; takes a few minutes each, in parallel)", flush=True)
    code = subprocess.run([PY, "orchestration/prepare.py", *todo], cwd=ROOT, env=env_for(cfg)).returncode
    if code == 0:
        cfg = config.load()
        cfg["prepared"] = sorted(set(cfg.get("prepared") or []) | set(todo))
        config.save(cfg)
    return say(code == 0, "workspaces bootstrapped", "see the output above; re-run ./start to retry the failed ones")


def launched(cfg: dict, tree: str) -> bool:
    code = subprocess.run([PY, "orchestration/launch.py", tree], cwd=ROOT, env=env_for(cfg, tree)).returncode
    return say(code == 0, f"builds launched for {tree}", "see the output above; re-run ./start to launch the rest")


def _running(pattern: str) -> list[str]:
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return out.stdout.split()


def loop(cfg: dict) -> bool:
    (ROOT / "logs").mkdir(exist_ok=True)
    for name, pattern in (("supervise.sh", SUPERVISOR), ("keepalive.sh", KEEPALIVE)):
        if not _running(pattern):
            subprocess.Popen(["nohup", "bash", f"orchestration/{name}"], cwd=ROOT, env=env_for(cfg),
                             stdout=open(ROOT / f"logs/{name.split('.')[0]}.log", "a"), stderr=subprocess.STDOUT,
                             start_new_session=True)
            time.sleep(2)
    ok = bool(_running(SUPERVISOR))
    return say(ok, "the loop is running (supervisor -> driver, keepalive)", "see logs/supervise.log")


def status(cfg: dict) -> None:
    zips = sorted((ROOT / "ship-zips").glob("*.zip"))
    print(f"shipped zips: {len(zips)} in ship-zips/")
    log = ROOT / "logs/driver.log"
    if log.is_file():
        lines = [l for l in log.read_text(errors="replace").splitlines() if " SUMMARY " in l]
        if lines:
            summary = json.loads(lines[-1].split(" SUMMARY ", 1)[1])
            print("last cycle:", lines[-1][:19], {k: v[0] for k, v in summary.items()})
    print("loop:", "running" if _running(SUPERVISOR) else "stopped",
          "| driver:", "running" if _running(DRIVER) else "stopped")
    print("follow it: tail -f logs/driver.log   |   remote runs: ./arena coder runs")


def stop() -> None:
    for pattern in (SUPERVISOR, KEEPALIVE, DRIVER):
        for pid in _running(pattern):
            subprocess.run(["kill", pid])
    print("stopped the local loop; runs already on Coder finish on their own (./start resumes)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="./start", description=__doc__.split("\n\n")[0])
    ap.add_argument("--dump", help="absolute path of the trace dump (a folder with sessions/)")
    ap.add_argument("--workspaces", help="comma-separated Coder workspace names")
    ap.add_argument("--batch", help="batch name (its tasks go to envs-<name>/); default: the newest batch")
    ap.add_argument("--seeds", type=int, help="how many seeds the batch builds (default 40)")
    ap.add_argument("--task-workers", type=int, help="parallel designs per workspace (default 7)")
    ap.add_argument("--check", action="store_true", help="prerequisites only")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    args = ap.parse_args(argv)
    os.chdir(ROOT)
    cfg = config.load()
    if args.stop:
        stop()
        return 0
    if args.status:
        status(cfg)
        return 0
    print("settings")
    if args.dump:
        cfg["dump"] = str(Path(args.dump).expanduser().resolve())
    if args.workspaces:
        cfg["workspaces"] = [w.strip() for w in args.workspaces.split(",") if w.strip()]
    if args.task_workers:
        cfg["task_workers"] = args.task_workers
    if args.batch or not (cfg["batches"] or args.check):
        tree = config.tree_name(args.batch or time.strftime("batch-%Y%m%d"))
        cfg["batches"].setdefault(tree, {"wave": f"{tree}.json", "dump": cfg.get("dump"),
                                         "seeds": args.seeds or 40, "launched": {}})
        if args.seeds:
            cfg["batches"][tree]["seeds"] = args.seeds
    else:                                         # the batch added last (a --check alone creates none)
        tree = list(cfg["batches"])[-1] if cfg["batches"] else "(none yet)"
    config.save(cfg)
    say(True, f"batch {tree} | workspaces {config.workspaces(cfg)} | dump {cfg.get('dump') or '(not set)'}")
    if not check(cfg):
        print("\nfix the FAIL lines above, then run ./start again")
        return 2
    if args.check:
        print("\nready: run ./start to launch")
        return 0
    print("batch")
    for step in (lambda: seeds(cfg, tree), lambda: prepared(config.load()), lambda: launched(config.load(), tree),
                 lambda: loop(config.load())):
        if not step():
            return 1
    print()
    status(config.load())
    return 0


if __name__ == "__main__":
    sys.exit(main())
