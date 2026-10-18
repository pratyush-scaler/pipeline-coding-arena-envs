"""Bootstrap the configured Coder workspaces for a batch: this checkout and the dump at the same absolute paths,
Docker, the Harbor runtime, the authoring venv, the code sync (pipeline.benchmark.coder_run.bootstrap), and a
disk janitor that prunes old sandbox images above 60% disk use.

usage: python orchestration/prepare.py [<ws> ...]      (default: every configured workspace)
"""
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestration import config  # noqa: E402

CFG = config.load()
if CFG.get("dump"):
    os.environ["ARENA_DUMP"] = CFG["dump"]
os.chdir(ROOT)
from pipeline.cli import load_keys  # noqa: E402
load_keys(prompt_if_missing=False)
from pipeline.benchmark import coder as coder_mod, coder_run  # noqa: E402

JANITOR = r'''#!/bin/bash
# Every 5 minutes, above 60% disk: prune authoring sandbox images older than 20 minutes and the build cache.
while true; do
  use=$(df --output=pcent / | tail -1 | tr -dc 0-9)
  if [ "${use:-0}" -gt 60 ]; then
    sg docker -c "docker image prune -af --filter label=arena-coding-sandbox --filter until=20m" >/dev/null 2>&1
    sg docker -c "docker builder prune -af --filter until=20m" >/dev/null 2>&1
    echo "$(date -u +%FT%TZ) pruned at ${use}%"
  fi
  sleep 300
done
'''
START = ('chmod +x /tmp/arena-janitor.sh; if [ -f /tmp/arena-janitor.pid ] && kill -0 "$(cat /tmp/arena-janitor.pid)" 2>/dev/null; '
         'then echo janitor-running; else nohup setsid /tmp/arena-janitor.sh >>/tmp/arena-janitor.log 2>&1 < /dev/null & '
         'echo $! > /tmp/arena-janitor.pid; echo janitor-started; fi')


def prepare(ws: str, results: dict) -> None:
    started = time.time()
    for attempt in range(4):                      # Coder ssh is slow and flaky: retried
        try:
            info = coder_run.bootstrap(ws)
            coder_mod.ssh(ws, "cat > /tmp/arena-janitor.sh", stdin=JANITOR.encode(), timeout=300)
            coder_mod.ssh(ws, START, timeout=300)
            results[ws] = {"ok": True, "info": info, "seconds": round(time.time() - started)}
            return
        except Exception as exc:                  # noqa: BLE001 -- reported per workspace
            results[ws] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[-400:]}"}
            print(f"{ws}: attempt {attempt + 1} failed: {results[ws]['error']}", flush=True)
            time.sleep(30)


def main(argv: list[str]) -> int:
    targets = argv or config.workspaces(CFG)
    if not targets:
        print("no workspaces configured: ./start --workspaces ws1,ws2,...")
        return 2
    results: dict = {}
    threads = [threading.Thread(target=prepare, args=(ws, results)) for ws in targets]
    [t.start() for t in threads]
    [t.join() for t in threads]
    for ws in targets:
        r = results.get(ws) or {"ok": False, "error": "no result"}
        print(f"  {ws}: {'ready' if r['ok'] else 'FAILED ' + r['error']}", flush=True)
    return 0 if all((results.get(ws) or {}).get("ok") for ws in targets) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
