"""High confidence privacy preflight; reports locations, never matched values."""
from __future__ import annotations
import re
import ipaddress
import json
from pathlib import Path

PATTERNS = {
    # Generic sk- tokens need a boundary: risk-/task-/mask- identifiers are
    # ordinary document content. Keep detecting explicit provider prefixes
    # even when other text immediately precedes them.
    "credential": re.compile(r"(?:sk-(?:ant-[\w-]*|proj-)[A-Za-z0-9_-]{24,}|(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"),
    # Every domain label starts and ends with a letter or digit, and the local
    # part never begins right after a backslash. Both hold for any real address;
    # without them a systemd unit (`sshd-keygen@.service.wants`) and a Python
    # decorator after an escaped newline in JSON (`\n@app.get`) read as email
    # and excluded a-019 and a-027 (build 2026-09-26). An address that follows
    # an escape is still found: `\nJohn@mail.org` matches from `J`.
    # The local part starts on a letter or digit that no backslash precedes: a
    # diff marker or decorator (`+@st.cache_data`, `\n@app.get`) is not an
    # address, while `\nrossi@azienda.it` inside a JSON string still is.
    "email": re.compile(r"(?<!\\)[A-Za-z0-9][A-Za-z0-9._%+-]*@((?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,})"),
}

_SSH_IPV4 = re.compile(
    r"\bssh[ \t]+[A-Za-z0-9_.-]+@(?P<host>\d{1,3}(?:\.\d{1,3}){3})"
    r"(?![\w.])(?:[ \t]+-p[ \t]+(?P<port>\d{1,5})(?!\d))?")
_DOCUMENTATION_NETS = tuple(ipaddress.ip_network(net) for net in
    ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
#: Reserved documentation domains, and systemd unit types: `getty@tty1.service`
#: is an instantiated unit, and no mail domain ends in a unit suffix.
_EXAMPLE_DOMAINS = {"example.com", "example.org", "example.net", "example.invalid"}
_SYSTEMD_UNIT = re.compile(r"\.(?:service|socket|target|timer|mount|automount|path|slice|scope|device|swap)\Z")


def _not_an_address(domain: str) -> bool:
    domain = domain.lower()
    return domain in _EXAMPLE_DOMAINS or bool(_SYSTEMD_UNIT.search(domain))


SSH_EXAMPLE = "ssh example-user@192.0.2.10"
ABSENCE_NOTE = "[ABSENCE_NOTE_REDACTED]"
WEB_ACCESS_PLACEHOLDERS = {
    'WEB_PASSWORD': '[REDACTED_WEB_PASSWORD]',
    'WEB_SECRET_KEY': '[REDACTED_WEB_SECRET_KEY]',
}
# Only literal defaults in these paired access-setting assignments qualify.
# The SECURITY_DATA form is an upstream-anonymized getenv prefix in recovered
# documents. Usernames, role names, admin IDs and other business data do not.
_WEB_ACCESS_DEFAULT = re.compile(r'''^[ \t]*(?:self\.)?(?P<name>WEB_PASSWORD|WEB_SECRET_KEY)
    [ \t]*=[ \t]*(?:os\.getenv\([ \t]*|\[SECURITY_DATA\][ \t]*)
    (?P<key_quote>["'])(?P=name)(?P=key_quote)[ \t]*,[ \t]*
    (?P<quote>["'])(?P<value>[^"'\\\r\n]{1,512})(?P=quote)[ \t]*\)''', re.M | re.X)


def web_access_default_spans(text: str):
    """Yield exact literal-value spans, never treating a bare word as a secret."""
    for match in _WEB_ACCESS_DEFAULT.finditer(text):
        replacement = WEB_ACCESS_PLACEHOLDERS[match['name']]
        if match['value'] != replacement:
            yield match.start('value'), match.end('value'), replacement

# A deliberately narrow policy: named health notes in an explicit absentees
# string array, including JS embedded as an escaped JSON string. General prose,
# attendance counts, diagnoses in reference material and unrelated arrays do
# not qualify for automatic source-credit-preserving transformation.
_ABSENTEES = re.compile(r'''(?<![\w])(?:\\?["'])?absentees(?:\\?["'])?\s*:\s*\[(?P<items>(?:[^\[\]]|\[ABSENCE_NOTE_REDACTED\]){1,4096})\]''')
_ABSENCE_ENTRY = re.compile(r'''(?P<quote>\\?["'])(?P<note>[^"'\\\r\n]{1,200})(?P=quote)''')
_HEALTH_NOTE = re.compile(r"[^\W\d_][\w .-]{0,99}\s+[-–—]\s+(?:sick leave|medical leave)", re.I)


def health_absence_spans(text: str):
    """Yield note offsets, preserving array structure and surrounding bytes."""
    for array in _ABSENTEES.finditer(text):
        entries = list(_ABSENCE_ENTRY.finditer(array['items']))
        if not entries:
            continue
        cursor = 0
        valid = True
        for index, entry in enumerate(entries):
            separator = array['items'][cursor:entry.start()].strip()
            if separator != (',' if index else ''):
                valid = False
                break
            cursor = entry.end()
        if not valid or array['items'][cursor:].strip() not in {'', ','}:
            continue
        for entry in entries:
            if _HEALTH_NOTE.fullmatch(entry['note']):
                yield (array.start('items') + entry.start('note'),
                       array.start('items') + entry.end('note'))
_EXAM_PRIVATE = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?(?:\*\*)?"
    r"(?:Your Answer[ \t]*:|Marks Obtained\b|"
    r"Time Taken(?:\*\*)?[ \t]*(?::|$)|Rank(?:\*\*)?[ \t]*(?::|$)|\[PERSON_NAME_\d+\])",
    re.I | re.M)


def ssh_endpoints(text: str):
    """Recognize explicit access commands, without treating every IP as private."""
    for match in _SSH_IPV4.finditer(text):
        try:
            address = ipaddress.ip_address(match["host"])
        except ValueError:
            continue
        if address.is_loopback or any(address in net for net in _DOCUMENTATION_NETS):
            continue
        if match["port"] is not None and not 1 <= int(match["port"]) <= 65535:
            continue
        yield match


def redact_excerpt(text: str) -> str:
    """Trace excerpts may contain credentials absent from recovered fixtures."""
    for start, end, replacement in reversed(list(web_access_default_spans(text))):
        text = text[:start] + replacement + text[end:]
    for start, end in reversed(list(health_absence_spans(text))):
        text = text[:start] + ABSENCE_NOTE + text[end:]
    for match in reversed(list(ssh_endpoints(text))):
        text = text[:match.start()] + SSH_EXAMPLE + text[match.end():]
    for kind, pattern in PATTERNS.items():
        text = pattern.sub(lambda m: m.group() if kind == "email" and _not_an_address(m.group(1))
                           else f"[REDACTED_{kind.upper()}]", text)
    return text


#: Share of printable characters above which undecodable bytes are treated as
#: text in a legacy single-byte encoding, whose ASCII the patterns can still see.
_TEXTLIKE_PRINTABLE = 0.95


def _textlike(text: str) -> bool:
    if not text:
        return True
    if "\x00" in text:
        return False
    printable = sum(ch.isprintable() or ch in "\t\n\r\f" for ch in text)
    return printable / len(text) >= _TEXTLIKE_PRINTABLE


def _zip_members(raw: bytes) -> list[dict]:
    import io
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            return [{"name": info.filename, "readable": True}
                    for info in archive.infolist() if not info.is_dir()]
    except (zipfile.BadZipFile, OSError):
        return [{"name": "(archive)", "readable": False}]


def _archive_views(raw: bytes, name: str, members) -> tuple[list, list]:
    """One level of archive members, each scanned through its own text view."""
    from .dump.formats import inspect_body
    views, unscannable = [], []
    for entry in members:
        member = entry.get("name")
        if not isinstance(member, str) or member.endswith("/"):
            continue
        if not entry.get("readable"):
            unscannable.append((member, "archive member not safely readable"))
            continue
        try:
            view = inspect_body(raw, name, member=member)
        except ValueError as exc:
            unscannable.append((member, f"member unreadable ({type(exc).__name__})"))
            continue
        if isinstance(view.get("text"), str):
            views.append((f"member:{member}", view["text"]))
        else:
            unscannable.append((member, view.get("readability") or "binary member"))
    return views, unscannable


def text_views(path: Path) -> tuple[list[tuple[str | None, str]], list[tuple[str | None, str]]]:
    """Every text rendering of `path` the scan can see, and what it cannot.

    Returns `(views, unscannable)`: `views` are `(label, text)` pairs, where the
    label is `None` for a file read as text and names the derived view (`docx`,
    `pdf`, `member:<name>`) otherwise; `unscannable` are `(member, why)` pairs
    for content that exists but has no text we could extract. Zip-deflated
    office documents and Flate PDFs are opaque to a byte scan, so a scan that
    stopped at the bytes would pass them unread.
    """
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
        if "\x00" not in text:
            return [(None, text)], []
    except UnicodeDecodeError:
        pass
    from .dump.formats import inspect_body
    try:
        view = inspect_body(raw, path.name)
    except Exception as exc:  # inspection is best effort; silence is not
        view = {"format": "binary", "readability": f"inspection failed ({type(exc).__name__})"}
    fmt = view.get("format", "binary")
    # The byte scan is kept alongside every derived view: it is what the scan
    # always did, and uncompressed metadata can sit outside the text layer.
    legacy = raw.decode("utf-8", errors="replace")
    views: list[tuple[str | None, str]] = []
    unscannable: list[tuple[str | None, str]] = []
    if isinstance(view.get("text"), str):
        views.append((fmt, view["text"]))
        readability = str(view.get("readability") or "")
        if readability.startswith("no extractable text"):
            unscannable.append((None, f"{fmt}: {readability}"))
        if fmt in ("docx", "pptx", "xlsx"):
            # The document view covers body text only. Comments, document
            # properties and tracked-change authors live in other parts.
            member_views, member_gaps = _archive_views(raw, path.name, _zip_members(raw))
            views.extend(member_views)
            unscannable.extend(member_gaps)
    elif isinstance(view.get("members"), list):
        member_views, member_gaps = _archive_views(raw, path.name, view["members"])
        views.extend(member_views)
        unscannable.extend(member_gaps)
    elif not (fmt == "binary" and _textlike(legacy)):
        # Mostly-printable bytes in a single-byte encoding are exempt: the
        # patterns are ASCII and see everything a reader would.
        why = ("pixel content has no text layer to scan" if fmt == "image"
               else view.get("readability") or "no text view")
        unscannable.append((None, f"{fmt}: {why}"))
    views.append(("bytes", legacy))
    return views, unscannable


def _scan_text(rel: str, text: str, view: str | None) -> list[dict]:
    where = {"file": rel, **({"view": view} if view else {})}
    findings = []
    findings.extend({**where, 'kind': 'web-access-default-metadata',
                     'line': text.count('\n', 0, start) + 1}
                    for start, _, _ in web_access_default_spans(text))
    findings.extend({**where, "kind": "health-absence-metadata",
                     "line": text.count("\n", 0, start) + 1}
                    for start, _ in health_absence_spans(text))
    if "Exam Result" in text and (match := _EXAM_PRIVATE.search(text)):
        findings.append({**where, "kind": "exam-attempt-metadata",
                         "line": text.count("\n", 0, match.start()) + 1})
    findings.extend({**where, "kind": "ssh-access-metadata",
                     "line": text.count("\n", 0, match.start()) + 1}
                    for match in ssh_endpoints(text))
    for kind, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            if kind == "email" and _not_an_address(match.group(1)):
                continue
            findings.append({**where, "kind": kind,
                             "line": text.count("\n", 0, match.start()) + 1})
    return findings


#: A person's recorded look at content no scanner can read (an image), pinned to
#: the exact bytes: {"files": {"<fixture path>": {"sha256", "reviewer", "verdict"}}}.
VISUAL_REVIEW = "provenance/visual-review.json"


def scan_text(rel: str, text: str) -> list[dict]:
    """Privacy findings in one text the pipeline is about to write under `rel`.

    The designer's authored fixture bytes go through this before they land;
    it never returns matched values.
    """
    return _scan_text(rel, text, None)


def unresolved(bundle: Path, findings: list[dict]) -> list[dict]:
    """Findings still open for a bundle's fixture.

    An `unscannable` finding is closed only by a visual review of those exact
    bytes whose verdict is "no personal data". Everything else stays open.
    """
    import hashlib
    path = Path(bundle) / VISUAL_REVIEW
    try:
        reviewed = json.loads(path.read_text()).get("files", {}) if path.is_file() else {}
    except (OSError, ValueError):
        reviewed = {}
    fixture = Path(bundle) / "environment/repo"
    open_ = []
    for row in findings:
        entry = reviewed.get(row.get("file")) if row.get("kind") == "unscannable" else None
        target = fixture / str(row.get("file"))
        if (isinstance(entry, dict) and entry.get("verdict") == "no personal data"
                and entry.get("reviewer") and target.is_file()
                and entry.get("sha256") == hashlib.sha256(target.read_bytes()).hexdigest()):
            continue
        open_.append(row)
    return open_


def scan(root: Path) -> list[dict]:
    """Privacy findings under `root`, by location. Never returns matched values.

    A file whose content cannot be rendered as text yields an `unscannable`
    finding: "could not check" must never read as "nothing found".
    """
    findings = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            findings.append({"file": str(path.relative_to(root)), "kind": "symlink"})
            continue
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        views, unscannable = text_views(path)
        derived: set[str] = set()
        for label, text in views:
            for finding in _scan_text(rel, text, label):
                # The byte view repeats whatever a derived view already found.
                if label == "bytes" and finding["kind"] in derived:
                    continue
                derived.add(finding["kind"])
                findings.append(finding)
        findings.extend({"file": rel, "kind": "unscannable",
                         **({"member": member} if member else {}), "reason": why}
                        for member, why in unscannable)
    return findings
