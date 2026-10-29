"""Launch a batch's builds on the configured workspaces: its frozen wave and each shard's sessions are pushed to the
same absolute paths, then one staged `build` per workspace runs detached (pipeline.benchmark.coder_run).

Ranks are build's own 1-based ranks over the wave's sorted survivors, dealt round-robin to the workspaces so every
shard mixes tiers. Launched ranks are recorded in orchestration/config.json, so a re-run launches only the rest.

usage: python orchestration/launch.py <tree> [--ranks A-B] [--dry]
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestration import config  # noqa: E402

CFG = config.load()


def survivors(wave: Path) -> list[dict]:
    from pipeline import orchestrate
    from pipeline.select.categories import annotate
    rows = [r for r in json.loads(wave.read_text())["rows"]
            if r["classification"].get("survives") and r.get("tier", 4) <= CFG["max_tier"]]
    annotate(rows)
    rows.sort(key=orchestrate.wave_order)
    return rows


def verify_dump(batch: dict, rows: list[dict], dump: Path, coder_mod, coder_run) -> int:
    """Compare each launched session's files by size on its workspace (one ssh per workspace) and re-copy the ones
    that differ or are missing (2026-10-18: 7 workspaces each held one data.json cut off by a transfer timeout)."""
    bad = 0
    for ws, ranks in (batch.get("launched") or {}).items():
        sids = sorted({rows[r - 1]["session_id"] for r in ranks if r <= len(rows)})
        local = {str(p.relative_to(dump)): p.stat().st_size for sid in sids
                 for p in (dump / "sessions" / sid).rglob("*") if p.is_file()}
        found = coder_mod.ssh(ws, f"cd {shlex.quote(str(dump))} && find " + " ".join(shlex.quote(f"sessions/{s}") for s in sids)
                              + " -type f -printf '%s %p\\n' 2>/dev/null", timeout=300, check=False)
        remote = {}
        for line in found.stdout.decode(errors="replace").splitlines():
            size, _, rel = line.partition(" ")
            if size.isdigit():
                remote[rel] = int(size)
        wrong = sorted(k for k, n in local.items() if remote.get(k) != n)
        for rel in wrong:
            coder_run.rsync(ws, str(dump / rel), str(dump / rel), push=True, timeout=900, compress=True)
        bad += len(wrong)
        print(f"  {ws}: {len(local)} files checked, {len(wrong)} re-copied {wrong[:4]}", flush=True)
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tree")
    ap.add_argument("--ranks", default=None)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--verify-dump", action="store_true",
                    help="re-copy any session file of an already launched rank whose size differs on its workspace "
                         "(a transfer cut off mid-file fails that task's release preflight)")
    args = ap.parse_args(argv)
    batch = CFG["batches"][args.tree]
    dump = Path(batch.get("dump") or CFG["dump"])
    os.environ["ARENA_DUMP"] = str(dump)
    os.chdir(ROOT)
    from pipeline.cli import load_keys
    load_keys(prompt_if_missing=False)
    from pipeline.benchmark import coder as coder_mod, coder_run

    wave = config.WAVES / batch["wave"]
    rows = survivors(wave)
    lo, hi = (int(x) for x in (args.ranks or batch.get("ranks") or f"1-{len(rows)}").split("-"))
    hi = min(hi, len(rows))
    launched = {r for ranks in (batch.get("launched") or {}).values() for r in ranks}
    todo = [r for r in range(lo, hi + 1) if r not in launched]
    workspaces = config.workspaces(CFG)
    shards = {ws: [] for ws in workspaces}
    for i, rank in enumerate(todo):
        shards[workspaces[i % len(workspaces)]].append(rank)
    if args.verify_dump:
        return verify_dump(batch, rows, dump, coder_mod, coder_run)
    print(f"{len(rows)} survivors in {wave.name}; launching ranks {todo[:1] and f'{todo[0]}..{todo[-1]}' or 'none'} "
          f"over {len(workspaces)} workspaces ({len(launched)} already launched)")
    for ws, ranks in shards.items():
        print(f"  {ws}: {ranks}")
    if args.dry or not todo:
        return 0
    tree = ROOT / args.tree
    done: dict[str, list[int]] = {}

    def one(ws: str, ranks: list[int]) -> None:
        # Mount the dump's persistent mirror BEFORE pushing sessions into it (2026-10-30 audit): after a workspace
        # restart the mount is gone, and mounting it later hid the sessions just pushed to the root disk.
        coder_mod.ssh(ws, coder_run.persist_script([dump]), timeout=900)
        coder_mod.ssh(ws, f"mkdir -p {shlex.quote(str(wave.parent))} {shlex.quote(str(dump / 'sessions'))}", timeout=300)
        coder_run.rsync(ws, str(wave), str(wave), push=True, timeout=900, compress=True)
        for sid in sorted({rows[r - 1]["session_id"] for r in ranks}):
            for tries in range(6):            # a hung coder-ssh rsync is killed and resumed (--partial)
                try:
                    coder_run.rsync(ws, f"{dump / 'sessions' / sid}/", f"{dump / 'sessions' / sid}/", push=True,
                                    timeout=600, compress=True)
                    break
                except Exception as exc:      # noqa: BLE001
                    print(f"  rsync {ws} {sid[:8]} try {tries + 1}: {type(exc).__name__}", flush=True)
            else:
                raise RuntimeError(f"session {sid} never reached {ws}")
        cmd = [str(ROOT / ".venv/bin/python"), "-m", "pipeline.cli", "coder", "run", ws, "--staged", "--no-sync",
               "--env", f"ARENA_SHARD={ws}-{args.tree}", "--", "build", "-o", str(tree), "-c", str(wave),
               "--ranks", ",".join(map(str, ranks)), "--task-workers", str(CFG["task_workers"]),
               "--max-tier", str(CFG["max_tier"])]
        out = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ), timeout=1800)
        if out.returncode == 0:
            done[ws] = ranks
        print(f"  LAUNCH {ws} ranks={ranks} rc={out.returncode} {(out.stdout + out.stderr).strip().splitlines()[-1:]}",
              flush=True)

    def guarded(ws: str, ranks: list[int]) -> None:
        for attempt in range(5):              # Coder ssh is slow and flaky
            try:
                return one(ws, ranks)
            except Exception as exc:          # noqa: BLE001
                print(f"  RETRY {ws} attempt {attempt + 1}: {type(exc).__name__}: {str(exc)[-300:]}", flush=True)
                time.sleep(30)
        print(f"  GAVE UP {ws} ranks={ranks}", flush=True)

    threads = [threading.Thread(target=guarded, args=(ws, r)) for ws, r in shards.items() if r]
    [t.start() for t in threads]
    [t.join() for t in threads]
    cfg = config.load()
    record = cfg["batches"][args.tree].setdefault("launched", {})
    for ws, ranks in done.items():
        record[ws] = sorted(set(record.get(ws, [])) | set(ranks))
    config.save(cfg)
    return 0 if len(done) == len([1 for r in shards.values() if r]) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
