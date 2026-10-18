"""Push the batch's shared learning files to every workspace (same absolute paths): the decisive-trap ledger
designers read (pipeline.learning_signal) and each batch's operator trap-idea folder (harden --traps).
usage: python orchestration/push_artifacts.py <ws> [<ws> ...]"""
import sys
import threading
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestration import config  # noqa: E402
from pipeline.benchmark import coder, coder_run  # noqa: E402


def push(ws: str) -> None:
    ledger = ROOT / "pipeline-artifacts/waves/decisive-traps.json"
    items = [ledger] if ledger.is_file() else []
    items += [p for t in config.trees() if (p := config.trap_ideas(t)) and p.is_dir()]
    for item in items:
        for attempt in range(3):
            try:
                coder.ssh(ws, f"mkdir -p '{item.parent}'", timeout=300)
                if item.is_dir():
                    coder_run.rsync(ws, f"{item}/", f"{item}/", push=True, timeout=600, compress=True)
                else:
                    coder_run.rsync(ws, str(item), str(item), push=True, timeout=600, compress=True)
                break
            except Exception as exc:                 # noqa: BLE001 -- retried, then reported
                print(f"{ws}: {item.name} attempt {attempt + 1} failed: {type(exc).__name__}", flush=True)
    print(f"{ws}: pushed {[i.name for i in items]}", flush=True)


threads = [threading.Thread(target=push, args=(ws,)) for ws in sys.argv[1:]]
[t.start() for t in threads]
[t.join() for t in threads]
