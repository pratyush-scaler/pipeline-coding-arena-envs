"""Remote half of `coder.run_campaign`: one campaign, run where it was sent.

Reads {"bundle", "output", "kwargs", "env"} from stdin, puts `env` (model
credentials) into this process only, runs `harbor.run_campaign` exactly as a
local calibration would, and prints the report after a marker line.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    request = json.loads(sys.stdin.read())
    os.environ.update(request.get("env") or {})
    from .coder import REPORT_MARKER
    from .harbor import run_campaign
    report = run_campaign(Path(request["bundle"]), Path(request["output"]),
                          **(request.get("kwargs") or {}))
    print(REPORT_MARKER, flush=True)
    print(json.dumps(report), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
