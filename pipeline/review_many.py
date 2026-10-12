"""`arena review <bundles>` -- the build's own semantic review, run on demand.

User, 2026-10-04: "Are we sure all the failures are genuine model failures."
Hardening revisions were validated by the gate chain, which has no semantic
review: their new hidden tests were never read by the independent reviewer for
being settled by the published instruction. This runs exactly the review a build
runs (`orchestrate._review_candidate`: reuse a current receipt, otherwise the
other-vendor reviewer) over each bundle and prints the verdict and findings.
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    from .config import bundle_paths
    from .orchestrate import Budget, _review_candidate
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)
    bundles = []
    for raw in args.paths:
        path = Path(raw)
        bundles += [path] if (path / "task.toml").is_file() else list(bundle_paths(path))
    budget = Budget(max(1, args.workers))

    def one(bundle: Path):
        return bundle, _review_candidate(Path(bundle).resolve(), budget)

    worst = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for bundle, result in pool.map(one, bundles):
            print(json.dumps({"bundle": str(bundle), "verdict": result["verdict"],
                              "findings": [str(f)[:300] for f in result.get("findings", [])[:8]]}), flush=True)
            worst = max(worst, {"green": 0, "blocked": 1}.get(result["verdict"], 2))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
