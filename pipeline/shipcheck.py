"""Ship-time sweep: the defect classes Arena's review found, checked before a task ships.

User, 2026-10-07, after Arena's coding-task feedback (C-001..C-044): "ensure
these kind of issues are not present in the shipped tasks moving forward" and
"if these kind of issues are found while shipping a task then lets fix them and
then ship". Every class below reached a delivered zip because nothing looked
for it; each detector is the cheapest check that would have caught its case.

    grader.current        tests/grade.py is the pipeline's current runtime        (C-044)
    image.login_shell     a toolchain whose binaries sit outside Debian's login
                          PATH re-exports PATH for login shells                   (C-002, C-032)
    image.shell_parity    sh -c, sh -lc, bash -c and bash -lc resolve the same
                          toolchain binaries in the built agent image   [--image]
    image.quiet_start     every interpreter starts with empty stderr    [--image] (C-033)
    image.imports         every import the repository's code makes resolves in the
                          agent image; `pip check` is clean                [--image]
    repro.build/golden    both images build from the package alone (--no-cache) and
                          the reference scores 1.0, the fixture 0           [--repro]
    deps.declared         what the repository's manifests declare is installed    (C-015)
    docs.toolchain        no doc requires a newer toolchain than the image has    (C-020)
    fixture.outside_paths code does not read absolute paths the image never has   (C-030, C-036)
    ledger.omissions      files extraction left out that the fixture names are
                          declared in the ledger                                  (C-028, C-036)
    runs.refusals         no counted episode was refused by a guard the current
                          grader no longer refuses                                (C-044, C-015)
    runs.guardrail_share  no guardrail fails more than
                          policy.SHIP_GUARDRAIL_FAIL_SHARE_MAX of one model's
                          scored episodes unadjudicated                           (verification-library)
    runs.certified        genuine.json certifies the shipped campaigns and every
                          check no shipped episode passed                         (browser-canvas-010)
    level.name            the ledger's level, the package name and its folder agree

Every finding carries its fix ROUTE and the MEASUREMENT the fixed task needs
(user, 2026-10-07): a fix the agent can see -- image, fixture, deps -- is
re-screened 5+5 before it ships; a grader-only or hidden-only fix is re-graded
on the captured episodes, which measures those exact episodes exactly because
the agent never saw the tests.

    regenerate   the pipeline rebuilds the file from the spec (no model)
    fixture      an authored fixture or deps edit, declared as authored
    ledger       a provenance declaration; the task itself is unchanged
    hidden       a hidden-only case or guardrail repair (`harden --fast --fix`)
    adjudicate   a human decision, recorded in verification/shipcheck-waivers.json

A waiver is the human's, never the sweep's: `{"adjudicator": "human", "waivers":
[{"check", "subject", "reason", "at"}]}` beside the hack adjudications, outside
the artifact identity. The receipt (verification/shipcheck.json) is bound to the
artifact identity it swept.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from . import policy

SCHEMA = "arena-coding/shipcheck/1"
RECEIPT = "verification/shipcheck.json"
WAIVERS = "verification/shipcheck-waivers.json"

#: Debian bookworm's /etc/profile, line 7: the PATH every non-root login shell
#: starts from (read from the pinned python base, 2026-10-07).
DEBIAN_LOGIN_PATH = ("/usr/local/bin", "/usr/bin", "/bin", "/usr/local/games", "/usr/games")

#: Each pinned base image's own ENV PATH (`docker image inspect`, 2026-10-07).
BASE_PATH = {
    "python": "/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "node": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "go": "/go/bin:/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "rust": "/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "php": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}

#: The toolchain version each base image installs, for docs.toolchain.
IMAGE_VERSION = {"python": ("python", (3, 12)), "node": ("node", (22,)), "browser": ("node", (22,)),
                 "go": ("go", (1, 23)), "rust": ("rust", (1, 83)), "php": ("php", (8, 3)),
                 "c": ("python", (3, 12)), "shell": ("python", (3, 12))}

TEXT_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".php", ".rb", ".go", ".rs", ".sh",
                 ".bash", ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".html", ".vue", ".svelte", ".css")
DOC_NAMES = re.compile(r"(?i)^(readme|install|setup|contributing|getting[-_]started|development)[^/]*\.(md|rst|txt)$")
SKIP_PARTS = {"node_modules", ".git", "venv", ".venv", "__pycache__", "dist", "build", "vendor"}

#: A quoted absolute path under a home or host-only root. /app, /opt, /tmp,
#: /usr, /etc and /dev are the image's own.
OUTSIDE_PATH = re.compile(r"""(?<![\w.])['"`](/(?:home|Users|root|mnt|media|workspace|workspaces|data)/[^'"`\s]+)['"`]""")
PHP_DIRNAME = re.compile(r"""dirname\(\s*__DIR__\s*,\s*(\d+)\s*\)\s*\.\s*['"]([^'"]+)['"]""")
DOC_VERSION = re.compile(
    r"(?i)\b(python|node(?:\.js)?|go(?:lang)?|rust|php)\s*(?:version\s*)?v?(\d+(?:\.\d+)?)\s*"
    r"(\+|or (?:later|newer|higher|above)|and (?:later|newer|above)|or greater)")


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def finding(check: str, subject: str, detail: str, *, route: str, measure: str, evidence=None) -> dict:
    return {"check": check, "subject": subject, "detail": detail, "route": route, "measure": measure,
            "evidence": evidence or []}


def _json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _repo_files(repo: Path):
    for path in sorted(repo.rglob("*")):
        rel = path.relative_to(repo)
        if path.is_file() and not path.is_symlink() and not (set(rel.parts) & SKIP_PARTS):
            yield rel.as_posix(), path


# ---------------------------------------------------------------------------
# Package detectors (no Docker, no runs)
# ---------------------------------------------------------------------------

def grader_current(bundle: Path) -> list[dict]:
    from .author import runtime
    return [finding("grader.current", "tests/grade.py", p, route="regenerate", measure="regrade")
            for p in runtime.problems(bundle)]


def login_shell(bundle: Path) -> list[dict]:
    doc = _json(bundle / "environment/toolchain.json", {}) or {}
    name = doc.get("name")
    if not name or doc.get("login_shell_path"):
        return []
    path = (doc.get("env") or {}).get("PATH") or BASE_PATH.get(name) or BASE_PATH.get(
        "node" if name == "browser" else "python", "")
    lost = [d for d in path.split(":") if d and d not in DEBIAN_LOGIN_PATH and not d.endswith("sbin")]
    if not lost:
        return []
    return [finding("image.login_shell", name,
                    f"a login shell (Codex runs `/bin/sh -lc`) resets PATH and loses {', '.join(lost)}; "
                    "set spec.toolchain.login_shell_path and rebuild", route="regenerate", measure="screen",
                    evidence=[f"agent PATH {path}", f"Debian login PATH {':'.join(DEBIAN_LOGIN_PATH)}"])]


def _norm_py(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _py_requirements(text: str) -> set[str]:
    out = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")):
            continue
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if m:
            out.add(_norm_py(m.group(1)))
    return out


def _pyproject_requirements(text: str) -> set[str]:
    import tomllib
    try:
        doc = tomllib.loads(text)
    except ValueError:
        return set()
    names = set()
    project = doc.get("project") or {}
    for req in project.get("dependencies") or []:
        names |= _py_requirements(req)
    for reqs in (project.get("optional-dependencies") or {}).values():
        for req in reqs:
            names |= _py_requirements(req)
    poetry = ((doc.get("tool") or {}).get("poetry") or {})
    for section in ("dependencies", "dev-dependencies"):
        names |= {_norm_py(k) for k in (poetry.get(section) or {}) if k.lower() != "python"}
    return names


def _node_requirements(text: str) -> set[str]:
    try:
        doc = json.loads(text)
    except ValueError:
        return set()
    return {k for section in ("dependencies", "devDependencies") for k in (doc.get(section) or {})}


def deps_declared(bundle: Path) -> list[dict]:
    repo = bundle / "environment/repo"
    deps = bundle / "environment/deps"
    doc = _json(bundle / "environment/toolchain.json", {}) or {}
    installed_py = _py_requirements((deps / "requirements.txt").read_text()) \
        if (deps / "requirements.txt").is_file() else set()
    installed_node = _node_requirements((deps / "package.json").read_text()) \
        if (deps / "package.json").is_file() else set()
    out = []
    for rel, path in _repo_files(repo):
        name = path.name
        if re.fullmatch(r"requirements[\w.-]*\.txt", name):
            declared, installed, kind = _py_requirements(path.read_text(errors="replace")), installed_py, "python"
        elif name == "pyproject.toml":
            declared, installed, kind = _pyproject_requirements(path.read_text(errors="replace")), installed_py, "python"
        elif name == "package.json":
            declared, installed, kind = _node_requirements(path.read_text(errors="replace")), installed_node, "node"
            # A node toolchain installs the repo's own manifest, vendored and relinked.
            if doc.get("name") in ("node", "browser") and (deps / "package.json").is_file() \
                    and _sha(deps / "package.json") == _sha(path):
                continue
        else:
            continue
        missing = sorted(declared - installed)
        if missing:
            out.append(finding("deps.declared", rel,
                               f"{kind} packages the repository declares but the image does not install: "
                               + ", ".join(missing[:20]), route="fixture", measure="screen",
                               evidence=[f"installed from environment/deps: {len(installed)} packages"]))
    return out


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split("."))


def docs_toolchain(bundle: Path) -> list[dict]:
    doc = _json(bundle / "environment/toolchain.json", {}) or {}
    have = IMAGE_VERSION.get(doc.get("name"))
    if not have:
        return []
    language, version = have
    out = []
    for rel, path in _repo_files(bundle / "environment/repo"):
        if not (DOC_NAMES.match(path.name) or rel.startswith("docs/")):
            continue
        text = path.read_text(errors="replace")
        for m in DOC_VERSION.finditer(text):
            word = m.group(1).lower()
            word = "node" if word.startswith("node") else "go" if word.startswith("go") else word
            if word != language:
                continue
            wanted = _version_tuple(m.group(2))
            if wanted[:len(version)] > version[:len(wanted)]:
                line = text.count("\n", 0, m.start()) + 1
                out.append(finding("docs.toolchain", f"{rel}:{line}",
                                   f"says {m.group(0).strip()!r}; the image has {language} "
                                   f"{'.'.join(map(str, version))}", route="fixture", measure="screen"))
    return out


def _declared_omissions(bundle: Path) -> list[str]:
    ledger = _json(bundle / "provenance/ledger.json", {}) or {}
    return [str(o.get("path") or "") for o in ledger.get("recovery_omissions") or [] if isinstance(o, dict)]


def _declared(name: str, declared: list[str]) -> bool:
    base = name.rstrip("/").rsplit("/", 1)[-1]
    return any(d and (d == name or d.endswith("/" + base) or d == base) for d in declared)


def _advertised(repo: Path) -> str:
    """Text that tells a reader what to run: docs, shell scripts, Makefiles, npm scripts."""
    parts = []
    for rel, path in _repo_files(repo):
        if DOC_NAMES.match(path.name) or path.suffix in (".sh", ".bash") or path.name in ("Makefile", "justfile"):
            parts.append(path.read_text(errors="replace"))
        elif path.name == "package.json":
            parts.append(json.dumps((_json(path, {}) or {}).get("scripts") or {}))
    return "\n".join(parts)


def fixture_outside_paths(bundle: Path) -> list[dict]:
    """Code reading a path only the user's machine had.

    When the script is one the repository tells the reader to run, it can never
    pass (C-036: verify_all.sh -> verify_sources.py -> /home/user/...): guard it
    in the fixture. Otherwise it is the user's own helper, faithful to the
    trace: declare the omission in the ledger and leave the code alone.
    """
    repo = bundle / "environment/repo"
    declared = _declared_omissions(bundle)
    advertised = _advertised(repo)
    authored = (_json(bundle / "authoring/extraction/fixture-authored.json", {}) or {}).get("files") or {}
    out = []
    for rel, path in _repo_files(repo):
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = path.read_text(errors="replace")
        run = path.name in advertised
        # An advertised script is resolved only by re-authoring it (the guard),
        # which the designer declares; a declaration alone cannot make it run.
        if run and rel in authored and "guard" in str(authored[rel]).lower():
            continue
        targets = sorted({m.group(1) for m in OUTSIDE_PATH.finditer(text)
                          if run or not _declared(m.group(1), declared)})
        if targets:
            out.append(finding("fixture.outside_paths", rel,
                               f"reads {', '.join(targets[:4])}, which the image never has; "
                               + ("the repository tells the reader to run it, so guard it in the fixture"
                                  if run else "declare the omission in the ledger"),
                               route="fixture" if run else "ledger", measure="screen" if run else "none",
                               evidence=targets[:12]))
        if path.suffix.lower() == ".php":
            depth = len(Path(rel).parent.parts)
            for m in PHP_DIRNAME.finditer(text):
                up, tail = int(m.group(1)), m.group(2)
                if up > depth and not _declared(tail, declared):
                    line = text.count("\n", 0, m.start()) + 1
                    out.append(finding("fixture.outside_paths", f"{rel}:{line}",
                                       f"includes {tail.lstrip('/')} from {up} levels up, outside the repository; "
                                       "declare it as a host file the trace never had (or ship a stub)",
                                       route="ledger", measure="none"))
    return out


def ledger_omissions(bundle: Path) -> list[dict]:
    extraction = _json(bundle / "authoring/extraction/extraction.json")
    if extraction is None:
        return []
    from .ledger.faithfulness import extraction_exclusions
    declared = _declared_omissions(bundle)
    texts = [path.read_text(errors="replace") for _, path in _repo_files(bundle / "environment/repo")
             if path.suffix.lower() in TEXT_SUFFIXES or DOC_NAMES.match(path.name)]
    named = []
    for item in extraction_exclusions(bundle):
        base = item["path"].rsplit("/", 1)[-1]
        if not _declared(item["path"], declared) and any(base in t for t in texts):
            named.append(item["path"])
    if not named:
        return []
    return [finding("ledger.omissions", "provenance/ledger.json",
                    f"{len(named)} file(s) extraction left out are named by the fixture but not declared in "
                    "recovery_omissions", route="ledger", measure="none", evidence=named[:20])]


def coverage(bundle: Path) -> list[dict]:
    """The coverage critic's executed gaps, from its receipt for THIS version."""
    from . import coverage_critic
    from .artifacts import identity
    record = _json(bundle / coverage_critic.RECEIPT)
    try:
        current = identity(bundle)
    except (OSError, ValueError):
        current = None
    if record is None or record.get("artifact_sha256") != current:
        return [finding("coverage.critic", coverage_critic.RECEIPT,
                        "the coverage critic has not run on this version: "
                        "`python -m pipeline.coverage_critic <bundle>`", route="regenerate", measure="none")]
    return coverage_critic.findings(record)


def coverage_advisory(bundle: Path) -> list[dict]:
    """Gaps the judges demoted (policy.CRITIC_GAP_RULE): reported with the sweep, never counted as open."""
    from . import coverage_critic
    from .artifacts import identity
    record = _json(Path(bundle) / coverage_critic.RECEIPT)
    try:
        current = identity(bundle)
    except (OSError, ValueError):
        current = None
    if record is None or record.get("artifact_sha256") != current:
        return []
    return coverage_critic.advisory(record)


#: A Landlock ruleset built by the hidden suites, and the right it must handle. A ruleset that does not
#: handle LANDLOCK_ACCESS_FS_REFER denies EVERY cross-directory rename or link to the confined process
#: (EXDEV), even where writing is meant to be free (Arena feedback 2026-10-08, python-cli-downloader;
#: executed 2026-10-09 with the shipped fence: os.rename/os.replace between two subfolders -> EXDEV).
_LANDLOCK = re.compile(r"landlock|handled_access_fs|RESTRICT\s*=\s*44[46]|syscall\(\s*(?:ctypes\.c_long\()?\s*444\b",
                       re.I)
_REFER = re.compile(r"\bREFER\b|ACCESS_FS_REFER|FS_REFER|1\s*<<\s*13\b|\b8192\b|0x2000\b")


def sandbox_refer(bundle: Path) -> list[dict]:
    out = []
    suites = Path(bundle) / "tests/suites"
    if not suites.is_dir():
        return out
    for p in sorted(suites.rglob("*")):
        if not p.is_file() or p.suffix not in (".py", ".js", ".mjs", ".cjs", ".ts", ".go", ".sh", ".c"):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if _LANDLOCK.search(text) and not _REFER.search(text):
            out.append(finding("sandbox.refer", f"tests/suites/{p.relative_to(suites).as_posix()}",
                               "a Landlock ruleset that does not handle LANDLOCK_ACCESS_FS_REFER denies every "
                               "cross-directory rename/link to the code under test (EXDEV): handle REFER and grant it "
                               "beneath every tree the code may write, keeping only the grader's folders unreadable",
                               route="harden", measure="regrade"))
    return out


def critic_empty(bundle: Path) -> list[dict]:
    """An empty coverage-critic run is not evidence of coverage (Arena feedback 2026-10-08: the downloader
    and text-parsing packages shipped with `raw_chars: 0` and `gaps: []`, and Arena found gaps in both)."""
    from . import coverage_critic
    record = _json(Path(bundle) / coverage_critic.RECEIPT) or {}
    out = []
    for step, row in sorted((record.get("steps") or {}).items()):
        executed = [r for r in (row.get("proposals") or []) if isinstance(r, dict) and r.get("applied")] \
            if isinstance(row, dict) else []
        # Not only an empty answer: an answer that yielded no executed proposal (unparsable, refused, or every
        # edit failed to apply) is no evidence either (c-024-vanilla-js-webapp v15, 2026-10-09: 2176 characters,
        # zero proposals, recorded as "0 gaps" and released).
        if isinstance(row, dict) and not executed:
            out.append(finding("coverage.critic_empty", f"{coverage_critic.RECEIPT}#{step}",
                               f"the coverage critic executed no proposal for this step (raw_chars "
                               f"{row.get('raw_chars') or 0}, {len(row.get('proposals') or [])} proposal(s), none "
                               "applied and graded): re-run it -- a run that tested nothing proves no clause is enforced",
                               route="regenerate", measure="none"))
    return out


#: What makes a declared untested surface legitimate: the instruction does not settle it (a determinacy
#: probe answered undetermined / both readings defensible), or no hidden case can observe it. Feasibility
#: alone ("needs network", "not exercised", "only X is compared") is not a reason: Arena found executed
#: 1.0 counter-implementations behind such declarations (feedback 2026-10-08: webgl controls,
#: shopify price display, express-chat fallback, php MySQL half).
_DETERMINACY_REASON = re.compile(
    r"determinacy|undetermin|not settled|unsettled|does not settle|doesn't settle|no rule|either reading|"
    r"both readings|ambigu|cannot be observed|can(?:not|'t) observe|no hidden case can|unobservable|"
    r"not observable|non-?determinis|not determin|undefined by the instruction|instruction (?:never|does not) "
    r"(?:say|state|fix|define)", re.I)


def _published(text: str, instructions: str) -> bool:
    """Whether a declared surface is about a clause the instruction publishes: it says so, or it quotes
    at least five consecutive words of an instruction verbatim."""
    if re.search(r"\bpublished\b|the instruction (?:says|states|asks)|instruction sentence|stated (?:rule|clause)",
                 text, re.I):
        return True
    norm = " ".join(re.findall(r"\w+", instructions.lower()))
    for quote in re.findall(r"['\u2018\u201c\"]([^'\u2019\u201d\"]{12,})['\u2019\u201d\"]", text):
        words = re.findall(r"\w+", quote.lower())
        if len(words) >= 5 and any(" ".join(words[k:k + 5]) in norm for k in range(len(words) - 4)):
            return True
    return False


def untested_unjustified(bundle: Path) -> list[dict]:
    """ADVISORY (not in the sweep: a text heuristic, not executed evidence)."""
    record = _json(Path(bundle) / "verification/probes.json") or {}
    instructions = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                             for p in sorted(Path(bundle).glob("steps/*/instruction.md")))
    out = []
    for i, text in enumerate(record.get("untested_surfaces") or []):
        text = str(text)
        if _published(text, instructions) and not _DETERMINACY_REASON.search(text):
            out.append(finding("coverage.untested_unjustified", f"verification/probes.json#untested_surfaces/{i}",
                               "declared untested without a determinacy reason -- close it with a hidden case "
                               "(and its m_partial_ mutant) or show the instruction does not settle it: "
                               + text[:220], route="harden", measure="regrade"))
    return out


#: An instruction sentence telling the agent to leave a file as it is.
_PRESERVE = re.compile(r"\b(?:leave|unchanged|untouched|as it is|as is|keep (?:it|them|that|this)?\s*(?:the )?same|"
                       r"stays? (?:the )?same|don'?t (?:touch|change|edit|modify)|do not (?:touch|change|edit|modify)|"
                       r"for now|byte[- ]identical|not be (?:changed|edited|modified))\b", re.I)


def preservation_unguarded(bundle: Path) -> list[dict]:
    """A fixture file an instruction sentence says to leave alone must be protected by a hash-guard or named
    by a hidden suite (Arena feedback 2026-10-08, fastapi-backend: 'leave the html for now' and nothing
    compared frontend/index.html with the original)."""
    bundle = Path(bundle)
    manifest = _json(bundle / "tests/fixture-manifest.json", {}) or {}
    manifest = manifest.get("files", manifest) if isinstance(manifest, dict) else {}
    paths = [p for p in manifest if isinstance(p, str)]
    checks = (_json(bundle / "tests/checks.json", {}) or {}).get("checks") or []
    guarded = [str(x) for c in checks if (c.get("runner") or {}).get("type") == "hash-guard"
               for x in (c.get("runner") or {}).get("paths") or []]
    suites = bundle / "tests/suites"
    suite_text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in sorted(suites.rglob("*"))
                           if p.is_file() and p.suffix in (".py", ".js", ".mjs", ".cjs", ".ts", ".go", ".php", ".sh")) \
        if suites.is_dir() else ""
    out, seen = [], set()
    for inst in sorted(bundle.glob("steps/*/instruction.md")):
        text = inst.read_text(encoding="utf-8", errors="replace")
        for sentence in re.split(r"(?<=[.!?\n])\s+", text):
            if not _PRESERVE.search(sentence):
                continue
            low = sentence.lower()
            for rel in paths:
                name = rel.rsplit("/", 1)[-1]
                if "." not in name or len(name) < 5 or (rel.lower() not in low and not re.search(
                        r"(?<![\w.-])" + re.escape(name.lower()) + r"(?![\w-])", low)):
                    continue
                covered = any(rel == g or rel.startswith(g.rstrip("/") + "/") for g in guarded) \
                    or rel in suite_text or name in suite_text
                if not covered and rel not in seen:
                    seen.add(rel)
                    out.append(finding("coverage.preservation", rel,
                                       f"the instruction says to leave it ('{sentence.strip()[:120]}') but no hash-guard "
                                       "protects it and no hidden suite reads it", route="harden", measure="regrade"))
    return out


def step_timeouts(bundle: Path) -> list[dict]:
    """Every [[steps]] step names a verifier timeout covering all its checks timing out (policy.STEP_VERIFIER_MARGIN_S)."""
    import tomllib
    from .author.checks import STRUCTURAL
    try:
        doc = tomllib.loads((Path(bundle) / "task.toml").read_text())
    except (OSError, ValueError):
        return []
    checks = (_json(Path(bundle) / "tests/checks.json", {}) or {}).get("checks") or []
    out = []
    for step in doc.get("steps") or []:
        name = step.get("name")
        need = sum(float((c.get("runner") or {}).get("timeout_s", 120)) for c in checks
                   if c.get("step") == name and (c.get("runner") or {}).get("type") not in STRUCTURAL)
        have = (step.get("verifier") or {}).get("timeout_sec")
        if have is None or float(have) < need:
            out.append(finding("harbor.step_timeout", f"task.toml#{name}",
                               f"[steps.verifier] timeout_sec is {have} but the step's checks may take {need:.0f} s: "
                               "Harbor then applies its 600 s default and a hanging submission errors instead of "
                               "scoring 0 -- rebuild the package", route="regenerate", measure="none"))
    return out


#: A regex in a task's own hidden suites that matches the hidden tree without anchoring it at a path start:
#: `/tests/(suites|data)` also matches `./tests/data`, the agent's own folder, and a guardrail using it zeroes an
#: honest submission (c-072 review 2026-10-09: reference + a local run-tests helper scored 0.0, anchored 1.0).
_UNROOTED_TREE = re.compile(r"""(?:re\.compile\(|new RegExp\(|=\s*/)[^\n]*?(?<![\\\w])/tests/\(?(?:suites|data)""")
_ROOT_ANCHOR = re.compile(r"""\\A|\(\?:\\A|\[\\s|\(\?<!|\^""")


def suite_unrooted_scan(bundle: Path) -> list[dict]:
    out = []
    suites = Path(bundle) / "tests/suites"
    if not suites.is_dir():
        return out
    for p in sorted(suites.rglob("*")):
        if not p.is_file() or p.suffix not in (".py", ".js", ".mjs", ".cjs", ".ts"):
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _UNROOTED_TREE.search(line)
            if m and not _ROOT_ANCHOR.search(line[:m.end()]):
                out.append(finding("suite.unrooted_scan", f"tests/suites/{p.relative_to(suites).as_posix()}",
                                   "a hidden-tree pattern not anchored at a path start also matches the agent's own "
                                   f"./tests/... folders: {line.strip()[:160]}", route="harden", measure="regrade"))
                break
    return out


def level_name(bundle: Path) -> list[dict]:
    import tomllib
    ledger = _json(bundle / "provenance/ledger.json", {}) or {}
    level = ledger.get("faithfulness_level")
    try:
        name = tomllib.loads((bundle / "task.toml").read_text())["task"]["name"]
    except (OSError, ValueError, KeyError):
        return []
    m = re.search(r"\ba_f(\d)_", name)
    if not level or not m or not name.split("/")[-1].startswith("a_f"):
        return []
    out = []
    if f"F{m.group(1)}" != level:
        out.append(finding("level.name", name, f"package name says F{m.group(1)}, ledger says {level}",
                           route="regenerate", measure="none"))
    folder = bundle.parent.name
    if re.fullmatch(r"F\d-\w+", folder) and not folder.startswith(level):
        out.append(finding("level.name", folder, f"filed under {folder}, ledger says {level}",
                           route="regenerate", measure="none"))
    return out


# ---------------------------------------------------------------------------
# Image probe and reproduction (Docker)
# ---------------------------------------------------------------------------

def parse_image_probe(text: str) -> dict:
    from .image_probe import parse
    return parse(text)


def image_findings(probe: dict, granted_imports: set[str] = frozenset()) -> list[dict]:
    """Facts the agent image reported about itself, as findings.

    User, 2026-10-07: complete packages, every necessary import, nothing missing.
    """
    out = []
    for binary, by_shell in sorted(probe.get("which", {}).items()):
        seen = set(by_shell.values())
        if len(seen) > 1:
            out.append(finding("image.shell_parity", binary,
                               "resolves differently by shell mode: "
                               + "; ".join(f"{s} -> {v or 'missing'}" for s, v in sorted(by_shell.items())),
                               route="regenerate", measure="screen"))
    for binary, by_shell in sorted(probe.get("quiet", {}).items()):
        noisy = {s: v for s, v in by_shell.items() if v}
        if noisy:
            out.append(finding("image.quiet_start", binary,
                               "prints to stderr on an empty start: "
                               + "; ".join(f"{s}: {v[:160]}" for s, v in sorted(noisy.items())),
                               route="fixture", measure="screen"))
    for kind, label in (("py", "Python module"), ("node", "npm package"), ("composer", "Composer package")):
        for name, where in sorted(probe.get(kind, {}).items()):
            out.append(finding("image.imports", f"{kind}:{name}",
                               f"{label} {name!r} is imported by {where} but does not resolve in the agent image; "
                               "install it (pinned) or, when it can never run here, record a human waiver",
                               route="fixture", measure="screen"))
    for module, why in sorted(probe.get("go", {}).items()):
        out.append(finding("image.imports", f"go:{module}",
                           f"`go list -deps` cannot load {module} offline: {why[:200]}", route="fixture",
                           measure="screen"))
    for line in probe.get("pip", []):
        out.append(finding("image.packages", "pip check", line[:240], route="fixture", measure="screen"))
    return out


def image_probe(bundle: Path) -> tuple[list[dict], dict]:
    from .author import sandbox
    from .image_probe import script
    ok, why = sandbox.available()
    if not ok:
        return [finding("image.probe", "docker", f"not probed: {why}", route="adjudicate", measure="none")], {}
    run = sandbox.shell(bundle, script(), timeout=900)
    probe = parse_image_probe(str(run.get("stdout") or ""))
    if not probe["which"]:
        return [finding("image.probe", "agent image", "the probe printed nothing: "
                        + str(run.get("stderr") or "")[-300:], route="adjudicate", measure="none")], probe
    return image_findings(probe), probe


def reproduce(bundle: Path) -> tuple[list[dict], dict]:
    """Arena's reproduction, done first: both images from the package alone, then grade.

    `docker build --no-cache --pull` of environment/ and tests/ exactly as
    shipped (no image the pipeline cached, no file outside the two contexts),
    then the reference must score 1.0 and the untouched fixture 0.
    """
    import secrets
    import subprocess
    from .author import controls, sandbox
    ok, why = sandbox.available()
    if not ok:
        return [finding("repro.build", "docker", f"not reproduced: {why}", route="adjudicate", measure="none")], {}
    bundle = Path(bundle)
    stamp = secrets.token_hex(4)
    tags, record = {}, {"tags": {}}
    try:
        for role, context in (("agent", "environment"), ("verifier", "tests")):
            tag = f"arena-repro-{role}:{stamp}"
            proc = subprocess.run(["docker", "build", "--no-cache", "--pull", "-t", tag,
                                   "-f", str(bundle / context / "Dockerfile"), str(bundle / context)],
                                  capture_output=True, text=True, timeout=5400)
            if proc.returncode:
                return [finding("repro.build", f"{context}/Dockerfile",
                                "does not build from the package alone: " + (proc.stdout + proc.stderr)[-600:],
                                route="fixture", measure="screen")], record
            tags[role] = record["tags"][role] = tag
        steps = sandbox._steps(bundle)
        golden = sandbox.verify(bundle, controls._golden_variants(bundle, steps),
                                agent_tag=tags["agent"], verifier_tag=tags["verifier"])
        nop = sandbox.verify(bundle, {}, agent_tag=tags["agent"], verifier_tag=tags["verifier"])
        record.update(golden=golden.get("reward"), nop=nop.get("reward"))
        out = []
        if golden.get("reward") != 1.0:
            out.append(finding("repro.golden", "solution/", f"the reference scores {golden.get('reward')} on images "
                               "rebuilt from the package (expected 1.0)", route="adjudicate", measure="none"))
        if nop.get("reward") not in (0, 0.0):
            out.append(finding("repro.nop", "environment/repo", f"the untouched fixture scores {nop.get('reward')} "
                               "on images rebuilt from the package (expected 0)", route="adjudicate", measure="none"))
        return out, record
    finally:
        for tag in tags.values():
            subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True)


# ---------------------------------------------------------------------------
# Run detectors (the shipped campaigns)
# ---------------------------------------------------------------------------

def shipped_trials(runs: Path) -> list[dict]:
    """Every scored trial of the shipped campaigns: model, trial, step, grade record, capture."""
    runs = Path(runs)
    index = _json(runs / "index.json", {}) or {}
    root = runs.parent
    out = []
    for campaign in index.get("campaigns") or []:
        label = str(campaign.get("label") or "")
        if label == "reference":
            continue
        base = root / str(campaign.get("path") or "")
        for trial in campaign.get("trials") or []:
            if trial.get("exception"):
                continue
            tid = trial.get("trial_id")
            for step in sorted((trial.get("per_step_rubric") or {})):
                step_dir = base / "jobs/run" / str(tid) / "steps" / step
                grade = _json(step_dir / "verifier/arena-grade.json")
                if grade is None:
                    continue
                out.append({"model": label, "trial": tid, "step": step, "grade": grade,
                            "capture": step_dir / "artifacts/app/repo", "campaign": base.name})
    return out


def runs_refusals(bundle: Path, trials: list[dict]) -> list[dict]:
    """A guard refusal the CURRENT grader would not make is a false zero."""
    from .author.grader_runtime import grade as current
    from .evidence import grader_refusal
    checks = {c["id"]: c for c in (_json(bundle / "tests/checks.json", {}) or {}).get("checks", [])}
    manifest = _json(bundle / "tests/fixture-manifest.json", {}) or {}
    vendored = ((_json(bundle / "environment/toolchain.json", {}) or {}).get("vendored")) or []
    per_episode: dict[tuple, dict] = {}
    for t in trials:
        for cid, rec in (t["grade"].get("checks") or {}).items():
            check = checks.get(cid) or {}
            runner = (check.get("runner") or {}).get("type")
            refused = isinstance(rec, dict) and grader_refusal(rec)
            shadow = runner == "shadow-guard" and isinstance(rec, dict) and not rec.get("passed")
            if not (refused or shadow):
                continue
            if not t["capture"].is_dir():
                still = ["capture not retained"]
            elif shadow:
                ok, why = current.structural(check, t["capture"], manifest)
                still = [] if ok else [why]
            else:
                import shutil
                import tempfile
                with tempfile.TemporaryDirectory(prefix="shipcheck-") as tmp:
                    tree = Path(tmp) / "t"
                    shutil.copytree(t["capture"], tree, symlinks=True)
                    current.exclude_added_conftests(tree, manifest)
                    current.exclude_added_virtualenvs(tree, manifest)
                    if runner in ("pytest", "unittest"):
                        current.exclude_added_python_tests(tree, manifest)
                    # The same reachability rule the shipped grader applies (Arena feedback 2026-10-08).
                    still = (current.scan_problems(tree, manifest, vendored, check.get("runner") or {},
                                                   bundle / "tests/suites")
                             if hasattr(current, "scan_problems")
                             else current.introspection_problems(tree, manifest, vendored))
            reason = str(rec.get("error") or rec.get("reason") or "")[:240]
            key = (t["model"], t["trial"], t["step"])
            entry = per_episode.setdefault(key, {"still": set(), "checks": [], "reasons": set()})
            entry["checks"].append(cid)
            entry["reasons"].add(reason)
            entry["still"].update(still)
    out = []
    for (model, trial, step), entry in sorted(per_episode.items()):
        subject = f"{model}/{trial}/{step}"
        checks_text = f"{len(entry['checks'])} check(s): {', '.join(entry['checks'][:6])}"
        if entry["still"]:
            out.append(finding("runs.refusals", subject,
                               f"refused on {checks_text}; the current grader still refuses it: "
                               + "; ".join(sorted(entry["still"]))[:240],
                               route="adjudicate", measure="none", evidence=sorted(entry["reasons"])[:3]))
        else:
            out.append(finding("runs.refusals", subject,
                               f"a false refusal on {checks_text}: the current grader runs this capture; re-grade it",
                               route="regenerate", measure="regrade", evidence=sorted(entry["reasons"])[:3]))
    return out


def runs_guardrail_share(bundle: Path, trials: list[dict]) -> list[dict]:
    checks = {c["id"]: c for c in (_json(bundle / "tests/checks.json", {}) or {}).get("checks", [])}
    tally: dict[tuple[str, str], list[int]] = {}
    for t in trials:
        for cid, rec in (t["grade"].get("checks") or {}).items():
            if (checks.get(cid) or {}).get("kind") != "guardrail" or not isinstance(rec, dict):
                continue
            row = tally.setdefault((t["model"], cid), [0, 0])
            row[1] += 1
            row[0] += 0 if rec.get("passed") else 1
    out = []
    for (model, cid), (failed, total) in sorted(tally.items()):
        if total and failed / total > policy.SHIP_GUARDRAIL_FAIL_SHARE_MAX:
            out.append(finding("runs.guardrail_share", f"{model}/{cid}",
                               f"guardrail failed {failed}/{total} of {model}'s scored episodes; prove a careful "
                               "human reading the instruction keeps that behaviour, or repair the guardrail",
                               route="adjudicate", measure="regrade"))
    return out


def runs_certified(bundle: Path, trials: list[dict]) -> list[dict]:
    genuine = _json(bundle / "verification/genuine.json")
    if genuine is None:
        return [finding("runs.certified", "verification/genuine.json",
                        "no certificate: every counted failure must be certified genuine before shipping",
                        route="regenerate", measure="regrade")]
    out = []
    if not genuine.get("certified"):
        out.append(finding("runs.certified", "verification/genuine.json",
                           f"not certified: {(genuine.get('problems') or ['?'])[:2]}", route="adjudicate",
                           measure="regrade"))
    audited = {str(e.get("id")) for e in genuine.get("episodes") or []}
    shipped = {str(t["trial"]) for t in trials}
    missing = sorted(shipped - audited)
    if missing:
        out.append(finding("runs.certified", "verification/genuine.json",
                           f"{len(missing)} of {len(shipped)} shipped episodes are outside the certificate "
                           "(it certified another campaign); re-run genuine over the shipped campaigns",
                           route="regenerate", measure="regrade", evidence=missing[:12]))
    return out


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

def waivers(bundle: Path) -> list[dict]:
    doc = _json(Path(bundle) / WAIVERS, {}) or {}
    if doc.get("adjudicator") != "human":
        return []
    return [w for w in doc.get("waivers") or [] if isinstance(w, dict) and w.get("reason")]


def _waived(item: dict, granted: list[dict]) -> dict | None:
    return next((w for w in granted if w.get("check") == item["check"] and w.get("subject") == item["subject"]), None)


def sweep(bundle: Path, *, runs: Path | None = None, image: bool = False, repro: bool = False) -> dict:
    bundle = Path(bundle)
    found: list[dict] = []
    for detector in (grader_current, login_shell, deps_declared, docs_toolchain, fixture_outside_paths,
                     ledger_omissions, level_name, coverage, critic_empty,
                     preservation_unguarded, sandbox_refer, step_timeouts, suite_unrooted_scan):
        found += detector(bundle)
    probe, reproduced = {}, {}
    if image:
        extra, probe = image_probe(bundle)
        found += extra
    if repro:
        extra, reproduced = reproduce(bundle)
        found += extra
    runs = Path(runs) if runs else (bundle / "runs" if (bundle / "runs/index.json").is_file() else None)
    trials = shipped_trials(runs) if runs else []
    if runs:
        found += runs_refusals(bundle, trials) + runs_guardrail_share(bundle, trials) + runs_certified(bundle, trials)
    granted = waivers(bundle)
    for item in found:
        waiver = _waived(item, granted)
        if waiver:
            item["waived"] = waiver
    open_items = [f for f in found if not f.get("waived")]
    from .artifacts import identity
    try:
        ident = identity(bundle)
    except (OSError, ValueError):
        ident = None
    return {"schema": SCHEMA, "artifact_sha256": ident, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "runs": str(runs) if runs else None, "episodes": len({(t['model'], t['trial']) for t in trials}),
            "image_probed": bool(probe), "image": probe, "reproduced": reproduced, "findings": found, "open": len(open_items),
            "ok": not open_items, "measure": _measure(open_items), "advisory": coverage_advisory(bundle)}


def sweep_for_shipping(bundle: Path, runs: Path | None) -> dict:
    """The packaging sweep. The image probe and the from-scratch rebuild are reused
    from this version's own receipt when it was recorded for the IDENTICAL artifact
    (shipfix --finish runs both); anything else is run now. The run detectors always
    run fresh against the runs being shipped."""
    from .artifacts import identity
    bundle = Path(bundle)
    stored = _json(bundle / RECEIPT, {}) or {}
    try:
        current = identity(bundle)
    except (OSError, ValueError):
        current = None
    reuse = (stored.get("artifact_sha256") == current and stored.get("image_probed")
             and stored.get("reproduced"))
    report = sweep(bundle, runs=runs, image=not reuse, repro=not reuse)
    if reuse:
        carried = [f for f in stored.get("findings") or []
                   if str(f.get("check", "")).startswith(("image.", "repro."))]
        granted = waivers(bundle)
        for item in carried:
            waiver = _waived(item, granted)
            if waiver:
                item["waived"] = waiver
        report["findings"] += carried
        report["image"], report["reproduced"], report["image_probed"] = stored.get("image"), stored.get("reproduced"), True
        report["reused_from"] = stored.get("at")
        open_items = [f for f in report["findings"] if not f.get("waived")]
        report.update(open=len(open_items), ok=not open_items, measure=_measure(open_items))
    return report


def _measure(items: list[dict]) -> str:
    """The strongest re-measurement the open findings need once fixed."""
    kinds = {f["measure"] for f in items}
    return "screen" if "screen" in kinds else "regrade" if "regrade" in kinds else "none"


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("bundles", nargs="+", type=Path)
    ap.add_argument("--runs", type=Path, default=None, help="a delivered runs/ folder (default <bundle>/runs)")
    ap.add_argument("--image", action="store_true", help="probe the built agent image: shells, imports, packages")
    ap.add_argument("--repro", action="store_true", help="rebuild both images from the package with --no-cache and grade")
    ap.add_argument("--write", action="store_true", help=f"write {RECEIPT}")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    failed = 0
    for bundle in args.bundles:
        report = sweep(bundle, runs=args.runs, image=args.image, repro=args.repro)
        if args.write:
            from .artifacts import atomic_json
            atomic_json(bundle / RECEIPT, report)
        if args.json:
            print(json.dumps(report, indent=1, default=str))
            continue
        print(f"{bundle}: {'clean' if report['ok'] else str(report['open']) + ' open'}"
              f" ({report['episodes']} episodes swept; re-measure: {report['measure']})")
        for f in report["findings"]:
            mark = "waived" if f.get("waived") else f["route"]
            print(f"  [{mark:10s}] {f['check']:22s} {f['subject']}: {f['detail'][:200]}")
        failed += 0 if report["ok"] else 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
