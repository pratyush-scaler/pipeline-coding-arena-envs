"""One naming convention, applied everywhere Arena sees a task.

    a_f3_<task-name>

The faithfulness level is part of the delivered name (Arena's convention, the
same as the parent programme). Working-tree bundles are `c-NNN-<slug>`; the
`c-` prefix says "coding" and the rank is our own ordering, so neither
travels into the delivered name.

    working tree   target-F3-replay/c-004-ledger-totals
    full name      arena/a_f3_ledger-totals
    delivered at   F3-replay/a_f3_ledger-totals/

Harbor validates `[task] name` against `^[a-zA-Z0-9][a-zA-Z0-9._-]*/...$`, so
the `arena/` org prefix is mandatory.
"""
from __future__ import annotations

import re
from pathlib import Path

ORG = "arena"
SHORT_NAME = re.compile(r"^a_f[0-3]_[a-z0-9]+(?:-[a-z0-9]+)*$")
WORKING = re.compile(r"^c-?(\d+)-")
LEVELS = ("F0", "F1", "F2", "F3")
LEVEL_KEY = "faithfulness_level"


def slug(text: str) -> str:
    text = re.sub(r"^a_f[0-3]_", "", str(text))
    text = re.sub(r"^arena[/_-]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^[ac]-?\d+-", "", text)
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text or "task"


def short_name(level: str, name: str) -> str:
    level = str(level).upper()
    if level not in LEVELS:
        raise ValueError(f"faithfulness level must be one of {LEVELS}, not {level!r}")
    return f"a_{level.lower()}_{slug(name)}"


def package_name(level: str, name: str) -> str:
    return f"{ORG}/{short_name(level, name)}"


def parse(name: str) -> dict | None:
    candidate = name.split("/")[-1]
    if not SHORT_NAME.match(candidate):
        return None
    _, level, rest = candidate.split("_", 2)
    return {"level": level.upper(), "task": rest, "short_name": candidate}


def problems(bundle: Path, level: str | None = None) -> list[str]:
    import tomllib
    from .author.contracts import strict_json
    issues = []
    try:
        config = tomllib.loads((Path(bundle) / "task.toml").read_text())
    except (OSError, ValueError) as exc:
        return [f"task.toml is unreadable: {exc}"]
    name = (config.get("task") or {}).get("name", "")
    parsed = parse(name)
    if parsed is None:
        return [f"task name {name!r} does not follow a_f<level>_<task-name>; expected something like "
                f"{package_name('F2', Path(bundle).name)!r}"]
    if level is None:
        ledger_path = Path(bundle) / "provenance/ledger.json"
        if ledger_path.is_file():
            level = strict_json(ledger_path.read_text()).get("faithfulness_level")
    if level and parsed["level"] != str(level).upper():
        issues.append(f"task name claims faithfulness {parsed['level']} but the ledger measures {str(level).upper()}")
    stamped = (config.get("metadata") or {}).get(LEVEL_KEY)
    if stamped is not None and str(stamped).upper() != parsed["level"]:
        issues.append(f"task.toml [metadata].{LEVEL_KEY} is {stamped!r} but the task name claims {parsed['level']}")
    return issues


def apply(bundle: Path, level: str) -> dict:
    import tomllib
    from .artifacts import identity
    bundle = Path(bundle)
    path = bundle / "task.toml"
    if not path.is_file():
        return {"changed": False, "name": None, "skipped": "no task.toml"}
    text = path.read_text()
    before = tomllib.loads(text)["task"]["name"]
    after = package_name(level, bundle.name)
    if before == after:
        return {"changed": False, "name": after}
    path.write_text(rename_text(text, after))
    try:
        sha = identity(bundle)
    except ValueError:
        sha = None
    return {"changed": True, "previous": before, "name": after, "artifact_sha256": sha}


def rename_text(text: str, name: str) -> str:
    pattern = re.compile(r"(\[task\][^\[]*?\bname\s*=\s*)(\"[^\"]*\")", re.S)
    updated, count = pattern.subn(lambda m: m.group(1) + json_quote(name), text, count=1)
    if count != 1:
        raise ValueError("could not locate the [task] name in task.toml")
    return updated


def json_quote(value: str) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)


def set_metadata(text: str, key: str, value: str) -> str:
    import tomllib
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", key) or not isinstance(value, str):
        raise ValueError(f"not a plain metadata key/value: {key!r}")
    before = tomllib.loads(text)
    line = f"{key} = {json_quote(value)}"
    header = re.search(r"^\[metadata\][ \t]*$", text, re.M)
    if header is None:
        if "metadata" in before:
            raise ValueError("[metadata] is not a plain table header in task.toml")
        updated = text + ("" if not text or text.endswith("\n") else "\n") + f"\n[metadata]\n{line}\n"
    elif key in (before.get("metadata") or {}):
        pattern = re.compile(rf"^{key}[ \t]*=[ \t]*\"[^\"\\\n]*\"[ \t]*$", re.M)
        tail, count = pattern.subn(lambda _: line, text[header.end():], count=1)
        if count != 1:
            raise ValueError(f"[metadata].{key} is not a single-line string")
        updated = text[:header.end()] + tail
    else:
        updated = text[:header.end()] + "\n" + line + text[header.end():]
    expected = dict(before, metadata={**(before.get("metadata") or {}), key: value})
    if tomllib.loads(updated) != expected:
        raise ValueError(f"setting [metadata].{key} would change more than that key")
    return updated


def delivered_toml(text: str, name: str, level: str) -> str:
    return set_metadata(rename_text(text, name), LEVEL_KEY, str(level).upper())


def unique_names(items: list[tuple[str, str]]) -> dict[str, str]:
    groups: dict[str, list[str]] = {}
    for level, name in items:
        groups.setdefault(short_name(level, name), []).append(name)
    out: dict[str, str] = {}
    for base, names in groups.items():
        if len(names) == 1:
            out[names[0]] = base
            continue
        for name in names:
            match = WORKING.match(name) or re.match(r"^a-?(\d+)-", name)
            suffix = match.group(1) if match else slug(name)[:4]
            out[name] = f"{base}-{suffix}"
    return out


def collisions(items: list[tuple[str, str]]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for level, name in items:
        groups.setdefault(short_name(level, name), []).append(name)
    return {base: sorted(names) for base, names in groups.items() if len(names) > 1}
