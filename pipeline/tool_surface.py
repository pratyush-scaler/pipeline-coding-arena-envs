"""What the agent could actually do, recorded beside every rate it produced.

`pipeline/calibrate` is a cheap screen: it hands the model `list_files`,
`read_file` and `write_file` over the provider API. The shipped package declares
something else entirely -- `environment/tools.json` names Harbor's Terminus-2,
whose single action drives a tmux session, so the released task is solved by an
agent with a shell.

Both are legitimate. A screen is supposed to be cheaper than the thing it
screens for. What is not legitimate is a `pass@1` that does not say which agent
produced it, because the two are not comparable and nothing in the number says
so. This batch learned that the expensive way: a-554 was read as an infeasible
task because six episodes refused it -- "I couldn't complete the validation
reliably with the available read/write-only tools. The extract requires
aggregation across 6,879 outlets" -- when a shell agent would have run one `awk`
over the file. The refusal measured the screen, not the task.

So every rollout record carries `tool_surface`, and `matches` is false whenever
the screen's surface differs from the one the package ships. It is the same
house rule as classifying every zero: a measurement whose conditions are not
recorded is indistinguishable from one taken under the conditions you assumed.
"""
from __future__ import annotations

import json
from pathlib import Path

#: Written by `author/build.py` when a package declares an action surface.
TOOL_SCHEMA = "environment/tools.json"


def declared(bundle: Path) -> dict:
    """The action surface the package ships, or why we could not read it.

    An unreadable schema is never reported as agreement: `matches` stays false
    and the reason travels with it, because "could not check" must not render
    as "nothing wrong".
    """
    path = Path(bundle) / TOOL_SCHEMA
    if not path.is_file():
        return {"tools": [], "agent": None,
                "unavailable": f"{TOOL_SCHEMA} is missing; the package declares no action surface"}
    try:
        schema = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return {"tools": [], "agent": None,
                "unavailable": f"{TOOL_SCHEMA} is unreadable ({type(exc).__name__}: {exc})"}
    tools = [t.get("name") for t in schema.get("tools") or [] if isinstance(t, dict)]
    return {"tools": sorted(n for n in tools if isinstance(n, str)),
            "agent": (schema.get("harness") or {}).get("agent")}


def measured(tools) -> list[str]:
    """Tool names the screening harness actually exposed this run."""
    names = []
    for tool in tools or ():
        name = tool.get("name") if isinstance(tool, dict) else None
        if isinstance(name, str):
            names.append(name)
    return sorted(names)


def reconcile(bundle: Path, tools, agent: str | None = None) -> dict:
    """`tool_surface` for a record: what ran, what ships, and whether they agree.

    `matches` answers one question only -- may this rate be read as a rate for
    the released task? Anything less than an exact agreement on the action names
    means no, including the case where we could not read the package at all.
    """
    package = declared(bundle)
    ran = measured(tools)
    same = bool(ran) and not package.get("unavailable") and ran == package["tools"]
    surface = {"measured": ran, "measured_agent": agent,
               "declared": package["tools"], "declared_agent": package["agent"],
               "matches": same}
    if package.get("unavailable"):
        surface["warning"] = ("cannot tell whether this rate reflects the released task: "
                              + package["unavailable"])
    elif not same:
        surface["warning"] = (
            f"measured with {ran or 'no tools'} but the package ships "
            f"{package['tools'] or 'no tools'} ({package['agent']}); this is a SCREENING "
            "rate and must not be reported as a calibration rate for the released task")
    return surface
