"""Lift the working material for one coding seed out of its trace.

Deliberately dumb, as in the parent: it copies what the trace states and
records where every byte came from. For code the material is the whole
repository, not a handful of documents:

  before   the working tree as of the message boundary the fixture is taken
           at -- by default one message before the first correction, the
           state the user is about to dispute
  after    the working tree at the end of the seed -- the state the user was
           left with, candidate reference material, NEVER shipped
  moments  the corrections themselves, quoted, with their signal

Every text file under the tree is lifted (not only document extensions), plus
retained binaries under a size cap, minus vendored and generated directories
(`node_modules`, `.next`, `dist`, virtualenvs, caches) which the environment
re-creates from a pinned manifest. Each lifted file carries its snapshot
citation, so the ledger can credit it as `lifted` byte for byte.

`at_message` and `diff` serve the designer's workspace tools: it may lift the
fixture from any assistant turn, not only the pivot.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from ..config import sessions_root
from ..dump import recover as rec
from ..dump import signals as sig
from .contracts import contained

from ..config import VENDORED_PARTS as VENDORED  # noqa: E402  one definition, read by the index too

#: Placeholders the dump's upstream PII redaction leaves in file bodies
#: (`api_key = [SECURITY_DATA]"KEY", ...`). Inside a source file they are a
#: syntax error the trace's own agent never saw; the fixture must repair or
#: exclude them, and the ledger must know which bytes were touched.
import re as _re
REDACTION_MARKER = _re.compile(r"\[(?:SECURITY_DATA|REDACTED_[A-Z_]+|PATH_USERNAME_\d+|DEPLOY_SUBDOMAIN_\d+"
                               r"|[A-Z][A-Z]+(?:_[A-Z]+)*_\d+)\]")


def redaction_markers(text: str) -> int:
    return len(REDACTION_MARKER.findall(text))
MAX_TEXT_BYTES = 5 * 1024 * 1024
MAX_BINARY_BYTES = 2 * 1024 * 1024
MAX_FIXTURE_BYTES = 64 * 1024 * 1024


@dataclass
class Document:
    path: str                       # fixture-relative
    trace_path: str                 # as the trace named it
    msg_index: int
    source: str
    sha256: str
    bytes: int
    content: str | None = None      # text, or None for a binary
    blob: str | None = None         # `files/<hash>` reference for a binary
    snapshot: str | None = None


@dataclass
class Extraction:
    session_id: str
    seed_index: int
    span: tuple[int, int]
    pivot: int
    before: list[Document] = field(default_factory=list)
    after: list[Document] = field(default_factory=list)
    moments: list[dict] = field(default_factory=list)
    changed_paths: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    evidence_summary: dict = field(default_factory=dict)
    evidence_index: list[dict] = field(default_factory=list)
    #: Every user message of the session that carries text: {msg, sha256, chars, undone}.
    #: A multi-turn task cites one per step (`trace_turn`); validation checks the
    #: citation against this index (Spec Appendix A: turns are the user's own).
    user_turns: list[dict] = field(default_factory=list)
    source_root: Path | None = field(default=None, repr=False)

    def before_map(self) -> dict[str, Document]:
        return {d.path: d for d in self.before}

    def after_map(self) -> dict[str, Document]:
        return {d.path: d for d in self.after}


def usable_correction_count(row: dict, extraction: Extraction) -> int:
    declared = (row.get("classification") or {}).get("usable_moments")
    if declared is not None:
        if type(declared) is not int or declared < 0:
            raise ValueError("usable_moments must be a nonnegative integer")
        return declared
    return sum(isinstance(m.get("user"), str) and bool(m["user"].strip()) for m in extraction.moments)


def clean_path(path: str) -> str:
    """Fixture-relative path: strip the sandbox roots the trace carries."""
    p = (path or "").replace("\\", "/").lstrip("/")
    for root in ("home/user/", "app/", "workspace/", "mnt/data/", "tmp/"):
        if p.startswith(root):
            p = p[len(root):]
            break
    return p or "unnamed"


def vendored(path: str) -> bool:
    return any(part in VENDORED for part in PurePosixPath(path).parts)


#: `git clone` options that take a separate value token.
_CLONE_VALUE_FLAGS = frozenset({
    "-b", "--branch", "--depth", "-o", "--origin", "--reference", "--reference-if-able",
    "-c", "--config", "-j", "--jobs", "--filter", "--template", "--separate-git-dir",
    "--shallow-since", "--shallow-exclude", "-u", "--upload-pack", "--server-option",
    "--bundle-uri", "--recurse-submodules",
})
_REMOTE_URL = ("https://", "http://", "git://", "ssh://", "git@")
_SHELL_SEPARATORS = {"&&", "||", ";", "|", "&"}


def _clone_targets(command: str) -> list[tuple[str, str]]:
    """Every `(url, directory)` a shell command clones from a REMOTE URL.

    A local path (`git clone /srv/x`, `git clone ../y`) is the user's own
    material and is not reported.
    """
    import shlex
    # Separators glued to a token (`x.git;`, `y&&`) would otherwise ride along
    # into the URL or the target name.
    padded = _re.sub(r"(\|\||&&|;|\||&)", r" \1 ", command)
    try:
        tokens = shlex.split(padded, posix=True)
    except ValueError:
        tokens = padded.split()
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        if tokens[i] != "git":
            i += 1
            continue
        j = i + 1
        while j < len(tokens) and tokens[j].startswith("-") and tokens[j] not in _SHELL_SEPARATORS:
            j += 2 if tokens[j] in ("-C", "-c", "--git-dir", "--work-tree") else 1
        if j >= len(tokens) or tokens[j] != "clone":
            i = j if j > i else i + 1
            continue
        j += 1
        positional: list[str] = []
        while j < len(tokens) and tokens[j] not in _SHELL_SEPARATORS:
            token = tokens[j]
            if token == "--":
                j += 1
                positional.extend(t for t in tokens[j:] if t not in _SHELL_SEPARATORS)
                break
            if token.startswith("-"):
                j += 2 if token in _CLONE_VALUE_FLAGS else 1
                continue
            positional.append(token)
            j += 1
        if positional and positional[0].startswith(_REMOTE_URL):
            url = positional[0].rstrip("/")
            target = positional[1] if len(positional) > 1 else url.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
            if len(positional) == 1 and target.endswith(".git"):
                target = target[:-4]
            out.append((url, target))
        i = j
    return out


#: Revision sources that show the session WROTE a path (edit/write tools and
#: recorded write intents), as opposed to snapshots and reads.
def _written(revision) -> bool:
    source = str(getattr(revision, "source", "") or "")
    return (getattr(revision, "origin", "") == "input"
            or any(k in source for k in ("edit", "write", "create", "patch")))


def upstream_clones(trace: dict, ws: "rec.Workspace | None" = None) -> dict[str, dict]:
    """Directories the session cloned from a remote repository and never wrote to.

    A user who runs `git clone https://github.com/x/y` inside the session
    puts a third-party project into the working tree. The dump retains only
    the files the trace's tools touched, so what reaches us is a partial
    clone that cannot build, and its bytes are not the user's work: the first
    pilot seed carrying one (whisper.cpp under `whisper_cpp_repo/`) would
    have credited 6,030,076 of its 6,107,704 fixture bytes -- 98.7% -- as
    lifted fidelity for code the user never wrote. Such a directory is
    vendored material: excluded from the fixture like `node_modules`, named
    to the designer with its URL, and re-installed from a pinned source
    (`toolchain.extra_apt`, a pinned wheel, an authored stub) only when the
    task genuinely needs it.

    The remote URL alone does not decide it. A user who clones THEIR OWN
    repository and then works inside it (`Outlook_AnyFinder`: 395 edit and
    write revisions under the clone, nothing outside it) has made it the
    fixture. So with `ws` given, a clone the session wrote into is kept; only
    a clone it merely read or built is upstream. Without `ws`, every remote
    clone is reported (the caller decides).

    Keyed by the fixture-relative directory; the value cites the message and
    the command so the exclusion is checkable.
    """
    found: dict[str, dict] = {}
    for index, message in enumerate(trace.get("messages") or []):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool-bash":
                continue
            command = (block.get("input") or {}).get("command") if isinstance(block.get("input"), dict) else ""
            for url, target in _clone_targets(str(command or "")):
                directory = clean_path(target).strip("/")
                if not directory or directory == "unnamed" or directory in found:
                    continue
                found[directory] = {"dir": directory, "url": url, "msg_index": index,
                                    "command": str(command)[:200], "written_revisions": 0}
    if ws is not None and found:
        for revision in ws.revisions:
            if not _written(revision):
                continue
            hit = _in_clone(clean_path(revision.path), found)
            if hit is not None:
                found[hit]["written_revisions"] += 1
        found = {d: info for d, info in found.items() if not info["written_revisions"]}
    return found


def _in_clone(clean: str, clones: dict[str, dict] | None) -> str | None:
    """The clone directory `clean` sits under, if any."""
    if not clones:
        return None
    for directory in clones:
        if clean == directory or clean.startswith(directory + "/"):
            return directory
    return None


def _binaries_at(ws: rec.Workspace, at: int, root: Path) -> dict[str, dict]:
    """Latest retained binary per path at or before `at`, blob present."""
    latest: dict[str, dict] = {}
    for raw in ws.raw_revisions:
        if raw.get("text_recovered") or raw["path"].startswith("@evidence/"):
            continue
        if raw["msg_index"] > at:
            continue
        key = rec.path_key(raw["path"])
        if key in latest and latest[key]["msg_index"] >= raw["msg_index"]:
            continue
        reference = raw.get("reference")
        blob = root / ws.session_id / reference if reference else None
        if blob is None or not blob.is_file():
            continue
        latest[key] = raw
    # A path deleted from the live tree by `at` must not resurrect: keep only
    # binaries whose last event at or before `at` was a presence.
    last_state: dict[str, bool] = {}
    for index, _, _, path, revision in ws._events():
        if index <= at:
            last_state[path] = revision is not None or path in latest and last_state.get(path, True)
    return {k: v for k, v in latest.items() if last_state.get(k, True)}


def tree_at(ws: rec.Workspace, at: int, root: Path,
            clones: dict[str, dict] | None = None) -> tuple[list[Document], dict]:
    """The working tree at a message boundary, as fixture documents.

    `clones` (see `upstream_clones`) names directories the session cloned
    from a remote repository; their files are excluded like vendored parts
    and accounted under `skipped["upstream_clones"]`.
    """
    docs, skipped = [], {"vendored": 0, "oversize": 0, "collision": 0,
                         "canonical_paths_preserved_to_avoid_collision": 0}
    clone_hits: dict[str, dict] = {d: {**info, "files": 0, "bytes": 0} for d, info in (clones or {}).items()}
    live = {}
    for path, revision in sorted(ws.at(at).items()):
        if vendored(path):
            continue
        if (hit := _in_clone(clean_path(path), clones)) is not None:
            clone_hits[hit]["files"] += 1
            clone_hits[hit]["bytes"] += len(revision.content.encode("utf-8"))
            continue
        live[path] = revision
    skipped["vendored"] += len(ws.at(at)) - len(live) - sum(h["files"] for h in clone_hits.values())
    skipped["upstream_clones"] = [h for _, h in sorted(clone_hits.items()) if h["files"]]
    # Two distinct trace paths may clean to one fixture path (`home/user/x.md`
    # and `app/x.md`). Neither may silently overwrite the other, so every
    # member of a colliding group keeps its canonical path instead.
    groups: dict[str, set[str]] = {}
    for path in live:
        groups.setdefault(clean_path(path), set()).add(path)
    colliding = {p for members in groups.values() if len(members) > 1 for p in members}
    skipped["collision"] += len(colliding)
    seen: dict[str, str] = {}
    for path, revision in live.items():
        data = revision.content.encode("utf-8")
        if len(data) > MAX_TEXT_BYTES:
            skipped["oversize"] += 1
            continue
        if path in colliding:
            clean = path.lstrip("/")
            skipped["canonical_paths_preserved_to_avoid_collision"] += 1
        else:
            clean = clean_path(path)
        seen[clean] = path
        docs.append(Document(clean, path, revision.msg_index, revision.source,
                             hashlib.sha256(data).hexdigest(), len(data), revision.content))
    # Binaries never enter a coding fixture. A repository graded by executed
    # tests is source; the user's uploaded screenshots and generated images
    # are evidence (readable through the evidence tools), and the privacy
    # scanner cannot clear them -- every one of them was refused before
    # authoring on the first pilot (2026-09-28). They are recorded here so the
    # designer can author an asset when a test genuinely needs one.
    skipped["binaries_excluded"] = []
    for key, raw in sorted(_binaries_at(ws, at, root).items()):
        if vendored(key):
            skipped["vendored"] += 1
            continue
        if (hit := _in_clone(clean_path(key), clones)) is not None:
            clone_hits[hit]["files"] += 1
            clone_hits[hit]["bytes"] += int(raw.get("bytes", 0) or 0)
            skipped["upstream_clones"] = [h for _, h in sorted(clone_hits.items()) if h["files"]]
            continue
        skipped["binaries_excluded"].append({"path": clean_path(key), "source": raw.get("source", "workspace-blob"),
                                             "sha256": raw.get("sha256"), "bytes": raw.get("bytes", 0),
                                             "msg_index": raw.get("msg_index")})
    return docs, skipped


def user_turns(trace: dict) -> list[dict]:
    """Index of the session's user messages that carry text. No text is kept:
    the hash ties a step's `trace_turn` to the exact message without copying
    the user's words into authoring files."""
    out = []
    for index, message in enumerate(trace.get("messages") or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(str(b.get("text") or "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = ""
        if not text.strip():
            continue
        out.append({"msg": index, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "chars": len(text), "undone": str(message.get("undone")) == "True"})
    return out


def extract(session_id: str, seed_index: int, root: Path | None = None,
            pivot: int | None = None) -> Extraction:
    root = root or sessions_root()
    trace = rec.load_trace(session_id, root)
    outcomes = sig.load_outcomes(session_id, root)
    seeds = {s.index: s for s in sig.build_seeds(trace, outcomes)}
    if seed_index not in seeds:
        raise KeyError(f"{session_id} has no seed {seed_index}")
    seed = seeds[seed_index]
    ws = rec.recover(trace, root)
    clones = upstream_clones(trace, ws)
    corrections = sorted(m.msg_index for m in seed.moments)
    if pivot is None:
        pivot = corrections[0] if corrections else seed.hi
    before, skipped_before = tree_at(ws, max(pivot - 1, 0), root, clones)
    after, skipped_after = tree_at(ws, seed.hi + 1, root, clones)
    if not before:
        # An early seed can start on an empty tree (the session's first turn
        # creates it). The state at the seed's own end is then the only
        # material, and the ledger still cites every byte.
        before, skipped_before = tree_at(ws, seed.hi + 1, root, clones)
        pivot = seed.hi + 1
    bmap = {d.path: d for d in before}
    amap = {d.path: d for d in after}
    changed = sorted(p for p in amap if p not in bmap or amap[p].sha256 != bmap[p].sha256)
    removed = sorted(p for p in bmap if p not in amap)
    from ..dump.evidence import Catalog
    catalog = Catalog(trace, ws, root, (seed.lo, seed.hi), pivot)
    markers = {d.path: redaction_markers(d.content) for d in before
               if d.content is not None and redaction_markers(d.content)}
    return Extraction(
        session_id=session_id, seed_index=seed_index, span=(seed.lo, seed.hi), pivot=pivot,
        before=before, after=after,
        evidence_summary=catalog.summary(),
        evidence_index=[{k: v for k, v in item.items() if k != "text"} for item in catalog.records.values()],
        source_root=root, changed_paths=changed, user_turns=user_turns(trace),
        moments=[{"msg": m.msg_index, "signal": m.signal, "lang": m.language_hint,
                  "user": m.user_text, "prior_assistant": m.prior_assistant} for m in seed.moments],
        stats={"before_files": len(before), "before_bytes": sum(d.bytes for d in before),
               "before_binaries": sum(1 for d in before if d.content is None),
               "after_files": len(after), "after_bytes": sum(d.bytes for d in after),
               "changed": len(changed), "removed": removed, "corrections": len(seed.moments),
               "skipped_before": skipped_before, "skipped_after": skipped_after,
               "canonical_paths_preserved_to_avoid_collision":
                   skipped_before.get("canonical_paths_preserved_to_avoid_collision", 0),
               "redaction_markers": markers,
               "recovery_omissions": ws.omissions[:200], "pivot_is_first_correction": bool(corrections)})


def write(extraction: Extraction, dest: Path) -> dict:
    """environment/repo/ is the agent's world; authoring/extraction/ is ours."""
    dest = Path(dest)
    repo = dest / "environment" / "repo"
    private = dest / "authoring" / "extraction"
    for d in (repo, private / "after"):
        d.mkdir(parents=True, exist_ok=True)
    root = extraction.source_root or sessions_root()
    total = 0
    provenance = []
    # A trace can record `x` as a file at one turn and `x/y` beneath it at
    # another; both cannot exist on disk (2026-10-03: `.journal_source` stopped
    # a seed at extraction with FileExistsError). The directory carries the
    # content; the file that collides with it is omitted and recorded.
    def parents(docs):
        return {parent for d in docs for parent in Path(d.path).parents if str(parent) != "."}
    before_dirs, after_dirs = parents(extraction.before), parents(extraction.after)
    collisions = sorted({d.path for d in extraction.before if Path(d.path) in before_dirs}
                        | {d.path for d in extraction.after if Path(d.path) in after_dirs})
    if collisions:
        extraction.stats["path_collisions"] = collisions
    for doc in extraction.before:
        if Path(doc.path) in before_dirs:
            continue
        target = contained(repo, doc.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if doc.content is not None:
            target.write_text(doc.content, encoding="utf-8")
        else:
            data = (root / extraction.session_id / doc.blob).read_bytes()
            if hashlib.sha256(data).hexdigest() != doc.sha256:
                raise ValueError(f"blob for {doc.path} does not match its recorded hash")
            target.write_bytes(data)
        total += doc.bytes
        if total > MAX_FIXTURE_BYTES:
            raise ValueError("fixture exceeds the size cap")
        provenance.append({"path": doc.path, "trace_path": doc.trace_path, "sha256": doc.sha256,
                           "bytes": doc.bytes, "msg_index": doc.msg_index, "source": doc.source,
                           "snapshot": doc.snapshot, "blob": doc.blob,
                           "citation": f"{extraction.session_id}::msg{doc.msg_index}::{doc.trace_path}"})
    for doc in extraction.after:
        if Path(doc.path) in after_dirs:
            continue
        target = contained(private / "after", doc.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if doc.content is not None:
            target.write_text(doc.content, encoding="utf-8")
        elif doc.blob:
            target.write_bytes((root / extraction.session_id / doc.blob).read_bytes())
    (private / "fixture-provenance.json").write_text(json.dumps(
        {"schema": "arena-coding/fixture-provenance/1", "session_id": extraction.session_id,
         "seed_index": extraction.seed_index, "pivot": extraction.pivot, "files": provenance}, indent=2))
    (private / "moments.json").write_text(json.dumps(extraction.moments, indent=2, ensure_ascii=False))
    (private / "user-turns.json").write_text(json.dumps(
        {"schema": "arena-coding/user-turns/1", "session_id": extraction.session_id,
         "pivot": extraction.pivot, "span": list(extraction.span),
         "turns": getattr(extraction, "user_turns", []) or []}, indent=2))
    (private / "evidence-index.json").write_text(json.dumps(
        {"summary": extraction.evidence_summary, "records": extraction.evidence_index}, indent=2, ensure_ascii=False))
    (private / "extraction.json").write_text(json.dumps({
        "session_id": extraction.session_id, "seed_index": extraction.seed_index,
        "span": list(extraction.span), "pivot": extraction.pivot, "changed_paths": extraction.changed_paths,
        "stats": extraction.stats,
        "before": [asdict(d) | {"content": None} for d in extraction.before],
        "after": [asdict(d) | {"content": None} for d in extraction.after]}, indent=2))
    return {"fixture_files": [d.path for d in extraction.before], "fixture_bytes": total,
            "private_dir": str(private), "stats": extraction.stats}


def at_message(session_id: str, msg_index: int, root: Path | None = None) -> list[Document]:
    """The working tree at any message boundary (designer tool)."""
    root = root or sessions_root()
    trace = rec.load_trace(session_id, root)
    ws = rec.recover(trace, root)
    docs, _ = tree_at(ws, msg_index, root, upstream_clones(trace, ws))
    return docs


def diff(session_id: str, a: int, b: int, root: Path | None = None) -> dict:
    """Which paths changed between two message boundaries, with unified diffs for text."""
    import difflib
    root = root or sessions_root()
    trace = rec.load_trace(session_id, root)
    ws = rec.recover(trace, root)
    clones = upstream_clones(trace, ws)
    before, _ = tree_at(ws, a, root, clones)
    after, _ = tree_at(ws, b, root, clones)
    bm, am = {d.path: d for d in before}, {d.path: d for d in after}
    out = {"added": [], "removed": [], "modified": [], "diffs": {}}
    for path in sorted(set(bm) | set(am)):
        if path not in bm:
            out["added"].append(path)
        elif path not in am:
            out["removed"].append(path)
        elif bm[path].sha256 != am[path].sha256:
            out["modified"].append(path)
            if bm[path].content is not None and am[path].content is not None:
                out["diffs"][path] = "".join(difflib.unified_diff(
                    bm[path].content.splitlines(keepends=True), am[path].content.splitlines(keepends=True),
                    fromfile=f"msg{a}/{path}", tofile=f"msg{b}/{path}"))[:20000]
    return out


def snapshot_boundaries(session_id: str, root: Path | None = None) -> list[dict]:
    """Every message boundary at which the tree changed: what the designer can lift."""
    root = root or sessions_root()
    trace = rec.load_trace(session_id, root)
    ws = rec.recover(trace, root)
    out, previous = [], None
    clones = upstream_clones(trace, ws)
    indices = sorted({e[0] for e in ws._events()})
    for index in indices:
        tree = {p: r.content for p, r in ws.at(index).items()
                if not vendored(p) and _in_clone(clean_path(p), clones) is None}
        digest = hashlib.sha256(json.dumps({p: hashlib.sha256(c.encode()).hexdigest()
                                            for p, c in tree.items()}, sort_keys=True).encode()).hexdigest()
        if digest != previous:
            role = trace["messages"][index]["role"] if index < len(trace.get("messages", [])) else "?"
            out.append({"msg_index": index, "role": role, "files": len(tree),
                        "bytes": sum(len(c.encode()) for c in tree.values())})
        previous = digest
    return out
