"""Recover observed file revisions from tool bodies, snapshots and file blobs.

Snapshots are anchored to their message or manifest node, never assigned to an
earlier seed. Their supplied bytes and referenced blobs are independently bound
in the provenance ledger. Retained binary bytes are distinct from text that the
authoring reader supports; unsupported format never means a lost file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator
from .snapshots import path_key

from ..config import (BODY_TOOLS_INPUT, BODY_TOOLS_OUTPUT, DELIVERABLE_EXT,
                      TEXT_EXT, sessions_root)

# Professional text formats (including LaTeX, reStructuredText and JSONL) are
# recoverable source documents even when absent from the code-oriented list.
RECOVERABLE_TEXT_EXT = TEXT_EXT | DELIVERABLE_EXT


@dataclass(frozen=True)
class Revision:
    """One observed state of one path, at one point in the message stream."""
    path: str
    msg_index: int
    content: str
    source: str          # the tool that revealed it
    origin: str          # "output" (authoritative post-state) | "input" (intent)


@dataclass
class Workspace:
    """Every revision of every path we can see, in message order."""
    session_id: str
    revisions: list[Revision] = field(default_factory=list)
    omissions: list[dict] = field(default_factory=list)
    raw_revisions: list[dict] = field(default_factory=list)
    recovery_inputs: dict[str, str] = field(default_factory=dict)
    declaration_differences: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    selected_revisions: list[Revision] = field(default_factory=list)
    events: list[tuple] = field(default_factory=list, repr=False)
    _histories: dict = field(default_factory=dict, init=False, repr=False)
    _history_count: int = field(default=-1, init=False, repr=False)

    def _grouped(self):
        if self._history_count != len(self.revisions):
            self._histories = {}
            for revision in self.revisions:
                self._histories.setdefault(path_key(revision.path), []).append(revision)
            self._history_count = len(self.revisions)
        return self._histories

    def provenance_revisions(self):
        from .evidence import source_revisions
        return self.revisions + source_revisions(self) + self.selected_revisions

    def paths(self) -> set[str]:
        return set(self._grouped())

    def source_paths(self) -> set[str]:
        return {p for p in self.paths() if ext(p) in RECOVERABLE_TEXT_EXT}

    def history(self, path: str) -> list[Revision]:
        return self._grouped().get(path_key(path), [])

    def at(self, msg_index: int) -> dict[str, Revision]:
        """The workspace as of `msg_index`: the last revision of each path at or
        before that point. This is what a fixture is lifted from -- the spec
        explicitly frees us from the trace's final state, so an intermediate
        snapshot is a first-class choice, not a fallback."""
        out: dict[str, Revision] = {}
        for index, _, _, path, revision in self._events():
            if index <= msg_index:
                if revision is None:out.pop(path, None)
                else:out[path] = revision
        return out

    def span(self, lo: int, hi: int) -> dict[str, Revision]:
        """Recoverable observations in the span, even if deleted afterwards.

        This measures source availability, not an end-state. Use `at` when a
        task needs the live workspace at a particular message boundary.
        """
        return {path_key(r.path): r for r in self.revisions if lo <= r.msg_index <= hi}

    def _events(self):
        return self.events or [(r.msg_index, 0, i, path_key(r.path), r)
                               for i, r in enumerate(self.revisions)]

    def bytes_recovered(self) -> int:
        return sum(max(len(r.content.encode("utf-8")) for r in self.history(p))
                   for p in self.source_paths())


def ext(path: str | None) -> str:
    return os.path.splitext(path or "")[1].lower()


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _path_of(inp: dict) -> str | None:
    for key in ("path", "file_path", "filename", "file"):
        v = inp.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def load_trace(session_id: str, root: Path | None = None) -> dict:
    root = root or sessions_root()
    from ..author.contracts import contained
    if not isinstance(session_id, str) or "/" in session_id or "\\" in session_id:
        raise ValueError("session_id must be one directory name")
    with contained(root, session_id + "/data.json").open() as fh:
        return json.load(fh)["traces"][0]


def recover(trace: dict, root: Path | None = None, *, include_snapshots: bool = True,
            include_context: bool = True) -> Workspace:
    """Merge channels at their observed times; retain explicit recovery gaps."""
    ws = Workspace(session_id=trace.get("session_id", "?"))
    for i, msg in enumerate(trace.get("messages", [])):
        for block in msg.get("content", []):
            btype = block.get("type", "")
            if not btype.startswith("tool-"):
                continue
            name = btype[5:]
            inp = _as_dict(block.get("input"))
            out = block.get("output")
            path = _path_of(inp)
            if path is None:
                continue
            try:path_key(path)
            except ValueError:
                ws.omissions.append({'msg_index': i, 'reason': 'unsafe_tool_path'})
                continue
            result = _as_dict(out)
            failed = (block.get("state") == "output-error" or bool(block.get("errorText"))
                      or result.get("success") is False or bool(result.get("error"))
                      or result.get("exit_code") not in (None, 0))
            truncated = any(result.get(k) for k in ("truncated", "is_truncated", "content_truncated"))
            if name in BODY_TOOLS_INPUT + BODY_TOOLS_OUTPUT and (failed or truncated):
                ws.omissions.append({"path": path, "msg_index": i,
                                     "reason": "failed_tool" if failed else "truncated_body"})
                continue
            if name in BODY_TOOLS_OUTPUT:
                body = _as_dict(out).get("content")
                if isinstance(body, str):
                    ws.revisions.append(Revision(path, i, body, name, "output"))
            elif name in BODY_TOOLS_INPUT:
                for key in ("content", "contents", "text", "data"):
                    body = inp.get(key)
                    if isinstance(body, str):
                        ws.revisions.append(Revision(path, i, body, name, "input"))
                        break
    ws.events = [(r.msg_index, 0, i, path_key(r.path), r)
                 for i, r in enumerate(ws.revisions)]
    if include_snapshots:
        from .snapshots import read
        # In-memory tool-only traces remain supported without a configured dump.
        has_references = any(m.get('workspace') for m in trace.get('messages', []))
        if root is not None or has_references:
            snapshots = read(trace, root or sessions_root())
            ws.omissions.extend(snapshots['omissions'])
            ws.recovery_inputs = snapshots['inputs']
            ws.declaration_differences = snapshots['declaration_differences']
            previous = set()
            for number, snapshot in enumerate(snapshots['snapshots']):
                index = snapshot['msg_index']
                for path in sorted(previous - snapshot['paths']):
                    ws.events.append((index, 1, number, path, None))
                for record in snapshot['files']:
                    revision = None
                    if 'sha256' in record:
                        ws.raw_revisions.append({k: record[k] for k in
                            ('path', 'sha256', 'bytes', 'source', 'reference', 'snapshot')} |
                            {'msg_index': index, 'text_recovered': isinstance(record.get('content'), str)})
                        if isinstance(record.get('content'), str):
                            revision = Revision(record['path'], index, record['content'], record['source'], 'snapshot')
                            ws.revisions.append(revision)
                    # A present but dropped/unreadable revision invalidates an
                    # older body for this path at this time, without erasing history.
                    ws.events.append((index, 1, number, record['key'], revision))
                previous = snapshot['paths']
    ws.events.sort(key=lambda event: event[:3])
    ws.revisions.sort(key=lambda revision: revision.msg_index)
    if include_context:
        from .evidence import read_channels
        context = read_channels(trace, root)
        ws.evidence = context['records']
        ws.recovery_inputs.update(context['inputs'])
        ws.omissions.extend(context['omissions'])
        for item in ws.evidence:
            if item['kind'] == 'upload' and item.get('status') == 'retained' and not item.get('undone'):
                ws.raw_revisions.append({'path': '@evidence/' + item['id'],
                    'msg_index': item['msg_index'], 'source': 'upload', 'snapshot': None,
                    'reference': item['reference'], 'sha256': item['sha256'],
                    'bytes': item['bytes'], 'text_recovered': False})
    return ws


def hash_trail(session_id: str, root: Path | None = None) -> dict[str, int]:
    """Distinct content hashes per path, from `workspaces/`.

    This covers paths the model wrote but never read back, so it is the honest
    denominator for "did we recover the files that actually changed?". It gives
    no content by itself; `recover` independently loads the associated bodies.
    """
    wsdir = (root or sessions_root()) / session_id / "workspaces"
    if not wsdir.is_dir():
        return {}
    hashes: dict[str, set[str]] = {}
    for name in sorted(os.listdir(wsdir)):
        if not name.endswith(".json"):
            continue
        try:
            with open(wsdir / name) as fh:
                snap = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        for f in snap.get("files", []):
            p, h = f.get("path"), f.get("hash")
            if p and h:
                hashes.setdefault(p, set()).add(h)
    return {p: len(h) for p, h in hashes.items() if len(h) >= 2}


def bash_health(trace: dict) -> dict[str, int]:
    """Truncation and command failure counts.

    `bash` output declares `stdout_truncated`/`stderr_truncated`, so truncation
    is read, never guessed. Do not regex for "truncat": it matches those keys
    and reports ~100% truncation on a corpus whose true rate is 1.27%.
    """
    total = truncated = failed = 0
    for msg in trace.get("messages", []):
        for block in msg.get("content", []):
            if block.get("type") != "tool-bash":
                continue
            out = _as_dict(block.get("output"))
            total += 1
            if out.get("stdout_truncated") or out.get("stderr_truncated"):
                truncated += 1
            if out.get("exit_code") not in (0, None):
                failed += 1
    return {"total": total, "truncated": truncated, "failed": failed}


def iter_sessions(root: Path | None = None) -> Iterator[str]:
    root = root or sessions_root()
    for name in sorted(os.listdir(root)):
        if (root / name).is_dir():
            yield name
