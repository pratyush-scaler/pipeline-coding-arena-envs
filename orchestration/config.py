"""One configuration for the whole loop: workspaces, the trace dump, and every batch.

`orchestration/config.json` is written by `./start` from its flags and read by the driver, the
launcher, packaging and the keepalive. It is per person and gitignored; config.example.json shows
the shape. Nothing here holds a credential (keys.env does, via `./arena keys`).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "orchestration/config.json"
WAVES = ROOT / "pipeline-artifacts/waves"
STATE_DIR = ROOT / "pipeline-artifacts/orchestration"
LOGS = ROOT / "logs"

DEFAULTS = {
    "workspaces": [],          # Coder workspace names the batch runs on
    "dump": "",                # absolute path of the trace dump (the same path is used on every workspace)
    "repro_workspace": "",     # where the Harbor reproduction gate runs (default: the first workspace)
    "task_workers": 7,         # parallel designs per workspace
    "max_tier": 4,
    "interval": 600,           # seconds between driver cycles
    "batches": {},             # tree name -> {"wave": file in pipeline-artifacts/waves, "dump": path, "ranks": "1-N",
                               #               "trap_ideas": optional directory, "launched": {ws: [ranks]}}
}


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))      # a deep copy: callers mutate cfg["batches"]
    if PATH.is_file():
        cfg.update(json.loads(PATH.read_text()))
    return cfg


def save(cfg: dict) -> None:
    PATH.write_text(json.dumps(cfg, indent=1) + "\n")


def tree_name(batch: str) -> str:
    """A batch's task tree under the checkout: envs-<batch> (letters, digits, dashes)."""
    slug = re.sub(r"[^a-z0-9-]+", "-", batch.lower()).strip("-")
    if not slug:
        raise ValueError("a batch needs a name of letters, digits or dashes")
    return slug if slug.startswith("envs-") else f"envs-{slug}"


def trees(cfg: dict | None = None) -> list[str]:
    return sorted((cfg or load()).get("batches") or {})


def waves(cfg: dict | None = None) -> dict[str, str]:
    return {t: b["wave"] for t, b in ((cfg or load()).get("batches") or {}).items() if b.get("wave")}


def dumps(cfg: dict | None = None) -> dict[str, str]:
    cfg = cfg or load()
    return {t: b.get("dump") or cfg.get("dump") for t, b in (cfg.get("batches") or {}).items()
            if b.get("dump") or cfg.get("dump")}


def trap_ideas(tree: str, cfg: dict | None = None) -> Path | None:
    folder = ((cfg or load()).get("batches") or {}).get(tree, {}).get("trap_ideas")
    return Path(folder) if folder else None


def workspaces(cfg: dict | None = None) -> list[str]:
    return list((cfg or load()).get("workspaces") or [])


def repro_workspace(cfg: dict | None = None) -> str:
    cfg = cfg or load()
    return cfg.get("repro_workspace") or (workspaces(cfg) or [""])[0]
