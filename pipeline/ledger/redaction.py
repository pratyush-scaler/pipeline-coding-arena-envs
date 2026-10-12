"""Reproducible data minimization against a recovered, hash-bound source.

Policies compute their own edits. An author cannot submit arbitrary replacement
text or offsets and receive anonymized-source credit. Private originals remain
in the recovered trace; reviewer receipts omit the removed values.
"""
from __future__ import annotations
import hashlib
import re
from pathlib import Path, PurePosixPath

from ..author.contracts import contained, strict_json
from ..dump import recover as rec
from ..privacy import ABSENCE_NOTE, SSH_EXAMPLE, health_absence_spans, ssh_endpoints, web_access_default_spans
import hashlib as _hashlib


def sha(data: bytes) -> str:
    return _hashlib.sha256(data).hexdigest()


def _keys(doc, allowed: set, what: str) -> None:
    """Refuse a document whose keys are not exactly `allowed`: a manifest with a
    key nothing reads is either stale or smuggling, and both are refused."""
    if not isinstance(doc, dict):
        raise ValueError(f"{what} must be a JSON object")
    if set(doc) != set(allowed):
        raise ValueError(f"{what} keys must be exactly {sorted(allowed)}, got {sorted(doc)}")

MANIFEST = "provenance/privacy-redaction.json"
SCHEMA = "arena-pipeline/privacy-redaction/1"
KINDS = {"ssh-access-metadata", "exam-attempt-metadata", "health-absence-metadata",
         "web-access-default-metadata", "network-device-identifiers"}

# -- network-device-identifiers --------------------------------------------
# A recovered SNMP walk carries the client MACs of a real network (as SNMP
# Hex-STRING values and as six-octet OID index suffixes), the controller's
# private address, the shell prompt's host and directory, and the SNMP user.
# a-062 (build 2026-09-26) was blocked on exactly that and no policy could clear
# it: recovered files are read-only to the author. Pseudonyms are a pure
# function of the file's content, so `verify` replays them exactly: MACs map
# into the RFC 7042 documentation block 00-00-5E-00-53-00/24 and private IPv4
# into TEST-NET-1 (192.0.2.0/24), one real value to one pseudonym in every form
# within a file. Placeholders (all-equal bytes, AA:BB:CC:DD:EE:FF ...) stay.
_HEX6 = re.compile(r"(?<=Hex-STRING:)(\s*)((?:[0-9A-Fa-f]{2}\s){5}[0-9A-Fa-f]{2})")
_OID_MAC = re.compile(r"(?<=\d\.)(\d{1,3}(?:\.\d{1,3}){5})(?=\s*=\s*Hex-STRING:\s*((?:[0-9A-Fa-f]{2}\s){5}[0-9A-Fa-f]{2}))")
_DEC6 = re.compile(r"(?<![\d.])\.?(\d{1,3}(?:\.\d{1,3}){5})(?![\d.])")
_COLON_MAC = re.compile(r"(?<![0-9A-Fa-f:-])[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4}(?![0-9A-Fa-f:-])")
# Two more spellings a Fable review found surviving in a-062's poller: a bare
# quoted hex string ("00 17 C8 D2 60 4D") and a Python octet list
# ([0,23,200,210,96,77]).
_BARE_HEX6 = re.compile(r"(?<![0-9A-Fa-f])((?:[0-9A-Fa-f]{2} ){5}[0-9A-Fa-f]{2})(?![0-9A-Fa-f])")
_INT_LIST6 = re.compile(r"\[\s*((?:\d{1,3}\s*,\s*){5}\d{1,3})\s*\]")
_PRIVATE_V4 = re.compile(r"(?<![\d.])(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}(?![\d.])")
_PROMPT = re.compile(r"([A-Za-z0-9._-]*)@([A-Za-z0-9._-]+):([~/][^\s$#\"\\]*)([$#])(?=\s)")
# The value is a literal name, never a format placeholder or variable (`%s`, `$USER`, `{u}`).
_SNMP_USER = re.compile(r"(?<![A-Za-z])(user|securityName|secName)=([^\s,)\"\\%${<][^\s,)\"\\]*)")
_PLACEHOLDER_MACS = {bytes.fromhex(h) for h in (
    "aabbccddeeff", "001122334455", "112233445566", "0123456789ab", "123456789abc")}
_DOC_MAC_PREFIX = bytes.fromhex("00005e0053")


def _logical_line(content: str, index: int) -> str:
    starts = [content.rfind(sep, 0, index) + len(sep) for sep in ("\n", "\\n")]
    ends = [e for e in (content.find("\n", index), content.find("\\n", index)) if e != -1]
    return content[max(starts):min(ends) if ends else len(content)]


def network_identifier_changes(content: str) -> list:
    """Typed, content-derived edits for network-device identifiers."""
    macs: dict[bytes, int] = {}
    ips: dict[str, int] = {}

    def mac_slot(raw: bytes) -> int:
        if raw not in macs:
            slot = hashlib.sha256(b"arena-mac:" + raw).digest()[0]
            while slot in macs.values():
                slot = (slot + 1) % 256
            macs[raw] = slot
        return macs[raw]

    def keep(raw: bytes) -> bool:
        return len(set(raw)) == 1 or raw in _PLACEHOLDER_MACS or raw.startswith(_DOC_MAC_PREFIX)

    def pseudo(raw: bytes) -> bytes:
        return _DOC_MAC_PREFIX + bytes([mac_slot(raw)])

    changes = []

    def add(start, end, kind_value):
        changes.append({"start": start, "end": end, "line": content.count("\n", 0, start) + 1,
                        "kind": "network-device-identifiers", "replacement": kind_value})

    for m in _HEX6.finditer(content):
        raw = bytes(int(x, 16) for x in m.group(2).split())
        if not keep(raw):
            add(m.start(2), m.end(2), " ".join(f"{b:02X}" for b in pseudo(raw)))
    for m in _OID_MAC.finditer(content):
        octets = [int(x) for x in m.group(1).split(".")]
        raw = bytes(int(x, 16) for x in m.group(2).split())
        if all(o < 256 for o in octets) and bytes(octets) == raw and not keep(raw):
            add(m.start(1), m.end(1), ".".join(str(b) for b in pseudo(raw)))
    taken = [(c["start"], c["end"]) for c in changes]
    for m in _DEC6.finditer(content):
        octets = [int(x) for x in m.group(1).split(".")]
        if (all(o < 256 for o in octets) and not keep(bytes(octets))
                and not any(s < m.end(1) and m.start(1) < e for s, e in taken)):
            add(m.start(1), m.end(1), ".".join(str(b) for b in pseudo(bytes(octets))))
    taken = [(c["start"], c["end"]) for c in changes]
    for m in _BARE_HEX6.finditer(content):
        raw = bytes(int(x, 16) for x in m.group(1).split())
        if not keep(raw) and not any(s < m.end(1) and m.start(1) < e for s, e in taken):
            add(m.start(1), m.end(1), " ".join(f"{b:02X}" for b in pseudo(raw)))
    for m in _INT_LIST6.finditer(content):
        octets = [int(x) for x in re.split(r"\s*,\s*", m.group(1).strip())]
        if all(o < 256 for o in octets) and not keep(bytes(octets)):
            sep = ", " if ", " in m.group(1) else ","
            add(m.start(1), m.end(1), sep.join(str(b) for b in pseudo(bytes(octets))))
    for m in _COLON_MAC.finditer(content):
        raw = bytes(int(x, 16) for x in re.split(r"[:-]", m.group(0)))
        if not keep(raw):
            sep, upper = m.group(1), m.group(0)[0:2].isupper() or m.group(0)[0:2].isdigit()
            add(m.start(), m.end(), sep.join((f"{b:02X}" if upper else f"{b:02x}") for b in pseudo(raw)))
    for m in _PRIVATE_V4.finditer(content):
        if m.group(0) not in ips:
            n = 1 + int.from_bytes(hashlib.sha256(b"arena-ipv4:" + m.group(0).encode()).digest()[:2]) % 254
            while n in ips.values():
                n = n % 254 + 1
            ips[m.group(0)] = n
        add(m.start(), m.end(), f"192.0.2.{ips[m.group(0)]}")
    for m in _PROMPT.finditer(content):
        add(m.start(), m.end(), f"{'user' if m.group(1) else ''}@host:~/work{m.group(4)}")
    for m in _SNMP_USER.finditer(content):
        if "snmp" in _logical_line(content, m.start()).lower():
            add(m.start(2), m.end(2), "snmp-user")
    return changes


def transform(content: str, kinds: list) -> tuple[str, list]:
    if (not isinstance(kinds, list) or not kinds or any(not isinstance(kind, str) for kind in kinds)
            or len(set(kinds)) != len(kinds)
            or any(kind not in KINDS for kind in kinds)):
        raise ValueError("unsupported or repeated redaction kind")
    changes = []
    if 'web-access-default-metadata' in kinds:
        for start, end, replacement in web_access_default_spans(content):
            changes.append({'start': start, 'end': end,
                'line': content.count('\n', 0, start) + 1,
                'kind': 'web-access-default-metadata', 'replacement': replacement})
    if "health-absence-metadata" in kinds:
        for start, end in health_absence_spans(content):
            changes.append({"start": start, "end": end,
                "line": content.count("\n", 0, start) + 1,
                "kind": "health-absence-metadata", "replacement": ABSENCE_NOTE})
    if "exam-attempt-metadata" in kinds:
        from .exam_privacy import changes as exam_changes
        changes.extend(exam_changes(content))
    if "network-device-identifiers" in kinds:
        changes.extend(network_identifier_changes(content))
    if "ssh-access-metadata" in kinds:
        for match in ssh_endpoints(content):
            changes.append({"start": match.start(), "end": match.end(),
                "line": content.count("\n", 0, match.start()) + 1,
                "kind": "ssh-access-metadata", "replacement": SSH_EXAMPLE})
    if not changes:
        raise ValueError("redaction policy found no supported private data")
    result, cursor = [], 0
    for change in sorted(changes, key=lambda row: row["start"]):
        if change["start"] < cursor:
            raise ValueError("redaction policies overlap")
        result.extend((content[cursor:change["start"]], change["replacement"]))
        cursor = change["end"]
    result.append(content[cursor:])
    return "".join(result), changes


def verify(bundle: Path, fixture: Path, ws: rec.Workspace, session_id: str) -> dict:
    path = contained(bundle, MANIFEST)
    if not path.exists():
        return {}
    fixture = contained(bundle, fixture.relative_to(bundle).as_posix())
    raw = path.read_bytes()
    manifest = strict_json(raw.decode())
    _keys(manifest, {"schema", "files"}, "redaction manifest")
    if manifest["schema"] != SCHEMA or not isinstance(manifest["files"], list) or not manifest["files"]:
        raise ValueError("unsupported or empty privacy redaction manifest")
    verified = {}
    for row in manifest["files"]:
        _keys(row, {"path", "citation", "source_sha256", "public_sha256", "kinds"}, "redaction file")
        rel = row["path"]
        public = contained(fixture, rel)
        if PurePosixPath(rel).as_posix() != rel or rel in verified:
            raise ValueError("redaction file paths must be canonical and unique")
        source = next((revision for revision in ws.provenance_revisions()
            if row["citation"] == f"{session_id}::msg{revision.msg_index}::{revision.path}"
            and sha(revision.content.encode()) == row["source_sha256"]), None)
        if source is None:
            raise ValueError(f"{rel}: redaction source hash/citation is absent from the trusted trace")
        transformed, changes = transform(source.content, row["kinds"])
        public_bytes = public.read_bytes()
        if transformed.encode() != public_bytes or sha(public_bytes) != row["public_sha256"]:
            raise ValueError(f"{rel}: fixture differs from the verified privacy redaction")
        verified[rel] = {"citation": row["citation"], "source_sha256": row["source_sha256"],
                         "public_sha256": row["public_sha256"], "changes": changes}
    return {"manifest_sha256": sha(raw), "policy": SCHEMA, "files": verified}


def audit(bundle: Path) -> dict:
    if not contained(bundle, MANIFEST).exists():
        return {}
    spec = strict_json((bundle / "authoring/spec.json").read_text())
    session = spec["source"]["session_id"]
    fixture = bundle / "environment" / "repo"
    if not fixture.is_dir():
        raise FileNotFoundError(f"no fixture directory at {fixture}")
    return verify(bundle, fixture, rec.recover(rec.load_trace(session), rec.sessions_root()), session)


def minimize(bundle: Path, session_id: str, root: Path | None = None) -> dict:
    """Apply supported policies before authoring; never accept an unbound edit.

    Call on an extracted working draft, before building/versioning evidence.
    All sources and transforms are checked before the first file is changed.
    Unsupported private data or ambiguous source shapes stop the build.
    """
    from ..artifacts import atomic_json
    from ..privacy import scan
    bundle = bundle.resolve()
    fixture = contained(bundle, "environment/repo")
    findings = scan(fixture)
    if not findings and not contained(bundle, MANIFEST).exists():
        return {}
    # Unscannable content (an image) is not something redaction can repair; it
    # is staged and stays an open privacy finding until a visual review of its
    # exact bytes is recorded (privacy.VISUAL_REVIEW). Release blocks until then.
    findings = [row for row in findings if row["kind"] != "unscannable"]
    if any(row["kind"] not in KINDS for row in findings):
        raise ValueError(f"source needs an unsupported privacy repair: {findings}")
    root = root or rec.sessions_root()
    ws = rec.recover(rec.load_trace(session_id, root), root)
    from ..dump.evidence import replay_selections
    replay_selections(ws, bundle, rec.load_trace(session_id, root), root)
    proof = verify(bundle, fixture, ws, session_id)
    if not findings:
        return proof
    manifest_path = contained(bundle, MANIFEST)
    manifest = strict_json(manifest_path.read_text()) if manifest_path.exists() else {"schema": SCHEMA, "files": []}
    by_file = {}
    for finding in findings:
        by_file.setdefault(finding["file"], set()).add(finding["kind"])
    plans = []
    for rel, kinds in sorted(by_file.items()):
        if rel in proof.get("files", {}):
            raise ValueError("existing privacy transformation needs explicit policy composition")
        path = contained(fixture, rel)
        original = path.read_bytes()
        source = next((rev for rev in reversed(ws.provenance_revisions()) if rev.content.encode() == original), None)
        if source is None:
            raise ValueError(f"{rel}: cannot minimize a fixture without an exact recovered source")
        public, _ = transform(source.content, sorted(kinds))
        plans.append((rel, path, public.encode()))
        manifest["files"].append({"path": rel,
            "citation": f"{session_id}::msg{source.msg_index}::{source.path}",
            "source_sha256": sha(original), "public_sha256": sha(public.encode()), "kinds": sorted(kinds)})
    # Calendar and text redaction on the same source require explicit replay
    # composition, not sequential credit inferred from two unrelated proofs.
    before = {rel: path.read_bytes() for rel, path, _ in plans}
    manifest_raw = manifest_path.read_bytes() if manifest_path.exists() else None
    try:
        for rel, path, public in plans:
            path.write_bytes(public)
        atomic_json(manifest_path, manifest)
        return verify(bundle, fixture, ws, session_id)
    except Exception:
        for rel, path, _ in plans:
            path.write_bytes(before[rel])
        if manifest_raw is None:
            manifest_path.unlink(missing_ok=True)
        else:
            manifest_path.write_bytes(manifest_raw)
        raise


def combine(*proofs: dict) -> dict:
    active = [proof for proof in proofs if proof]
    if not active:
        return {}
    if len(active) == 1:
        return active[0]
    files = {}
    for proof in active:
        if set(files) & set(proof["files"]):
            raise ValueError("multiple privacy policies on the same file need an explicit composition policy")
        files.update(proof["files"])
    import json
    manifests = [{key: proof[key] for key in ("policy", "manifest_sha256")} for proof in active]
    return {"policy": "arena-pipeline/privacy-composition/1", "files": files,
            "manifest_sha256": hashlib.sha256(json.dumps(manifests, sort_keys=True).encode()).hexdigest()}
