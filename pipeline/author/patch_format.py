"""Normalize unified-diff count metadata while preserving authored text."""
from __future__ import annotations

import copy
import re

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def normalize_counts(text: str) -> tuple[str, int]:
    """Count literal hunk lines; preserve positions and all non-header bytes.

    This does not apply patches, repair context, or execute submitted code.
    Unrecognized hunk bodies are left for normal validation to reject.
    """
    lines = text.splitlines(keepends=True)
    corrections = 0
    for index, line in enumerate(lines):
        newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        header = line[:-len(newline)] if newline else line
        match = _HUNK.fullmatch(header)
        if not match:
            continue
        old = new = 0
        valid = True
        for following in lines[index + 1:]:
            body = following.rstrip("\r\n")
            if _HUNK.fullmatch(body) or body.startswith("diff --git "):
                break
            # A plain multi-file diff may omit diff --git boundaries. Do not
            # guess whether paired file headers are authored program lines.
            if body.startswith(("--- ", "+++ ")):
                valid = False
                break
            if body.startswith(" "):
                old += 1
                new += 1
            elif body.startswith("-"):
                old += 1
            elif body.startswith("+"):
                new += 1
            elif body == "\\ No newline at end of file":
                continue
            else:
                valid = False
                break
        if not valid:
            continue
        declared_old = int(match[2]) if match[2] is not None else 1
        declared_new = int(match[4]) if match[4] is not None else 1
        if (old, new) == (declared_old, declared_new):
            continue
        lines[index] = f"@@ -{match[1]},{old} +{match[3]},{new} @@{match[5]}{newline}"
        corrections += 1
    return "".join(lines), corrections


def normalize_staged_patches(path: list, value):
    """Limit formatting to declared solution, alternative, and mutant patches."""
    result = copy.deepcopy(value)
    updates = []

    def entry(item, location):
        if isinstance(item, dict) and isinstance(item.get("patch"), str):
            formatted, count = normalize_counts(item["patch"])
            if count:
                item["patch"] = formatted
                updates.append({"path": [*location, "patch"], "hunk_counts_corrected": count})

    def section(name, item, location):
        if name in ("solution", "alternative") and isinstance(item, dict):
            for key, row in item.items():
                entry(row, [*location, key])
        elif name == "mutants" and isinstance(item, list):
            for index, row in enumerate(item):
                entry(row, [*location, index])

    if not path and isinstance(result, dict):
        for name in ("solution", "alternative", "mutants"):
            section(name, result.get(name), [name])
    elif path and path[0] in ("solution", "alternative", "mutants"):
        if len(path) == 1:
            section(path[0], result, path)
        elif len(path) == 2:
            entry(result, path)
        elif len(path) == 3 and path[-1] == "patch" and isinstance(result, str):
            result, count = normalize_counts(result)
            if count:
                updates.append({"path": list(path), "hunk_counts_corrected": count})
    return result, updates
