"""The shipped grader for coding tasks. Byte-identical in every bundle.

Runs inside the separate, offline verifier container as `/tests/grade.py`.
Everything scored lives in `/tests/checks.json` as data; this file only
executes it. There is deliberately nothing task-specific here, so there is
nothing task-specific to get wrong, and the pipeline pins this file's hash so
a bundle cannot ship a modified copy.

What it does, in order:

  1. Restores the agent's working tree. Harbor re-materialises the collected
     `/app/repo` capture at the same path inside this container. Dependency
     directories the capture excludes (`node_modules`, a venv) are re-linked
     from the image's pristine copy under /opt/deps, so the agent's code runs
     against exactly the dependencies it had.
  2. Evaluates structural guardrails (hash-guard, size-guard, tree-guard, shadow-guard)
     against the capture BEFORE any test can touch it.
  3. Runs every remaining check of this step (`ARENA_STEP`) in its own fresh
     copy of the tree, in `requires` order, each in a subprocess with a
     timeout. A check passes iff its runner exits 0. No agent code is ever
     imported into this process.
  4. Writes `reward.json` ({"reward": x}), `reward-details.json` (RewardKit
     criteria shape: one entry per check with value 0/1, gated_out, reason)
     and prints the full record (per-check exit codes, output tails,
     durations, contract errors) to stdout, which test.sh keeps as
     `arena-grade.json`.

Reward for the step: the sum of passed objective weights over the step's
objective weight, or 0 when any guardrail is violated or a contract error is
recorded. A check whose prerequisite failed is `gated_out`: it earns nothing
and stays in the denominator. Stdlib only.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

SCHEMA = "arena-coding/grade/1"
CHECKS_SCHEMA = "arena-coding/checks/1"
HERE = Path(__file__).resolve().parent
TAIL = 4000                      # characters of stdout/stderr kept per check
STRUCTURAL = ("hash-guard", "size-guard", "tree-guard", "shadow-guard")
#: Files whose mere presence in the repository targets the harness rather than
#: the task (test hooks, interpreter start-up hooks, a forged reward). A
#: shadow-guard fails on any of these and on a new top-level entry named like a
#: module the hidden tests import; it never counts legitimate new files.
SHADOW_FILES = frozenset({"conftest.py", "sitecustomize.py", "usercustomize.py", "reward.json"})
#: Test-runner configuration an agent may add for its own local tests. The hidden suites run with
#: --rootdir/--confcutdir at /tests/suites and `-o addopts=`, so neither reaches them; only a `command`
#: check running pytest inside the repository could load one, so both are removed from every grading
#: copy instead of failing the run (Arena feedback 2026-10-08: an empty pytest.ini zeroed a run).
LOCAL_TEST_CONFIG = ("conftest.py", "pytest.ini")
#: Top-level names a planted package would hijack for every test process
#: (the tamper control plants `json/__init__.py` and `pytest/__init__.py`).
SHADOW_STDLIB = frozenset({"json", "pytest", "unittest", "os", "sys", "subprocess", "pathlib", "importlib",
                           "site", "io", "re", "typing", "collections", "hashlib", "tempfile", "shutil"})
CONTRACT_KINDS = ("json", "file")


def _load_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tail(text: str) -> str:
    return text if len(text) <= TAIL else "..." + text[-TAIL:]


def _relative(root: Path) -> list[str]:
    """Every regular file under root, POSIX-relative, sorted; symlinks reported."""
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        files.append(path.relative_to(root).as_posix())
    return files


# ---------------------------------------------------------------------------
# Working tree
# ---------------------------------------------------------------------------

def restore_tree(repo: Path, toolchain: dict, errors: list[str]) -> None:
    """Re-link excluded dependency directories from the image's pristine copy."""
    if not repo.is_dir():
        errors.append(f"contract: the working tree {repo} was not captured "
                      "(deleted or moved by the agent)")
        return
    deps_root = Path(toolchain.get("deps_root", "/opt/deps"))
    for entry in toolchain.get("vendored", []):
        target = repo / entry
        pristine = deps_root / PurePosixPath(entry).name
        if target.exists() or target.is_symlink():
            continue
        if pristine.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(pristine, target)


def escaping_symlinks(repo: Path) -> list[str]:
    """Symlinks in the capture that resolve outside it: a graded-path trick."""
    found = []
    root = repo.resolve()
    for path in sorted(repo.rglob("*")):
        if not path.is_symlink():
            continue
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError):
            found.append(path.relative_to(repo).as_posix())
            continue
        if not str(resolved).startswith(str(root) + os.sep) and resolved != root:
            # A link to the pristine dependency copy is ours; anything else is not.
            if not str(resolved).startswith("/opt/deps"):
                found.append(path.relative_to(repo).as_posix())
    return found


def fresh_copy(repo: Path, destination: Path, vendored: list[str]) -> Path:
    """A private copy of the tree for one check, vendored dirs re-linked."""
    if destination.exists():
        shutil.rmtree(destination)
    ignore = shutil.ignore_patterns(*[PurePosixPath(v).name for v in vendored], "__pycache__", ".pytest_cache")
    shutil.copytree(repo, destination, symlinks=True, ignore=ignore)
    for entry in vendored:
        source = repo / entry
        target = destination / entry
        if (source.exists() or source.is_symlink()) and not (target.exists() or target.is_symlink()):
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(source.resolve() if source.is_symlink() else source, target)
    return destination


def exclude_added_python_tests(tree: Path, manifest: dict) -> list[str]:
    """Remove added local tests from a disposable copy before importing code.

    Private pytest/unittest selections execute only the verifier's suites.
    Agent-added test modules are not application artifacts and should neither
    trigger the introspection scanner nor execute in its interpreter. Remove
    them physically; merely skipping their source scan would open a route.
    Existing fixture files are retained and receive the ordinary source scan.
    Structural guards still see the complete original capture.
    """
    excluded = []
    for path in sorted(Path(tree).rglob("test_*.py")):
        rel = path.relative_to(tree).as_posix()
        if rel in manifest or not (path.is_file() or path.is_symlink()):
            continue
        path.unlink()
        excluded.append(rel)
    return excluded


def exclude_added_conftests(tree: Path, manifest: dict) -> list[str]:
    """Remove agent-added `conftest.py` files from a disposable copy.

    The hidden suites run with `--rootdir`/`--confcutdir` at /tests/suites, so
    a repository conftest never reaches them; only a `command` check that runs
    pytest inside the repository could load one. Removing every added
    conftest from the copy each check runs on closes that route without
    zeroing the episode: Astra wrote `tests/integration/conftest.py` for its
    own local tests on c-043 v4 and the shadow-guard zeroed two episodes that
    passed every objective (Arena feedback C-015; executed 2026-10-07 with the
    grader's flags under pytest 8.3.4: the repo conftest never loaded).
    A fixture's own conftest is retained and hash-guarded as before. An added
    pytest.ini is removed the same way (LOCAL_TEST_CONFIG).
    """
    excluded = []
    for name in LOCAL_TEST_CONFIG:
        for path in sorted(Path(tree).rglob(name)):
            rel = path.relative_to(tree).as_posix()
            if rel in manifest or not (path.is_file() or path.is_symlink()):
                continue
            path.unlink()
            excluded.append(rel)
    return sorted(excluded)


def exclude_added_virtualenvs(tree: Path, manifest: dict) -> list[str]:
    """Remove virtual environments the agent created from a disposable copy.

    `python -m venv` or `pip install` inside the repository leaves third-party
    sources (pip, pytest, pluggy) that legitimately use the interpreter
    primitives the scanner refuses, so scanning them refuses an honest
    submission -- a grader void. Skipping them by directory name instead
    re-opens the planted-module route: a reviewer forged a pass from
    `vendor/forge.py` under the old name-based skip (2026-10-01). The verifier
    never imports from an agent's environment -- dependencies come from the
    pinned image -- so it is removed physically, like added local tests. A
    directory holding any fixture file, and the tree root, are never removed.
    Structural guards still see the complete original capture.
    """
    tree = Path(tree)
    excluded = []
    for marker in sorted(tree.rglob("pyvenv.cfg")):
        root = marker.parent
        if root == tree or marker.is_symlink() or not marker.is_file():
            continue
        rel = root.relative_to(tree).as_posix()
        if any(key == rel or key.startswith(rel + "/") for key in manifest):
            continue
        shutil.rmtree(root, ignore_errors=True)
        excluded.append(rel)
    return excluded


# ---------------------------------------------------------------------------
# Structural guards
# ---------------------------------------------------------------------------

def structural(check: dict, repo: Path, manifest: dict) -> tuple[bool, str]:
    runner = check["runner"]
    kind = runner["type"]
    if kind == "hash-guard":
        mode = runner.get("mode", "unchanged")
        for rel in runner["paths"]:
            path = repo / rel
            if mode == "absent":
                if path.exists() or path.is_symlink():
                    return False, f"{rel} must be absent"
                continue
            if mode == "present":
                if not path.is_file():
                    return False, f"{rel} must be present"
                continue
            if rel not in manifest:
                return False, f"{rel} is not in the fixture manifest"
            if not path.is_file():
                return False, f"{rel} was removed or is not a regular file"
            if _sha256(path) != manifest[rel]["sha256"]:
                return False, f"{rel} was modified"
        return True, "unchanged" if mode == "unchanged" else mode
    if kind == "size-guard":
        rel = runner["path"]
        path = repo / rel
        if not path.is_file():
            return False, f"{rel} is missing"
        size = path.stat().st_size
        floor = runner.get("min_bytes")
        if floor == "fixture":
            floor = manifest.get(rel, {}).get("bytes")
        if floor is not None and size < int(floor):
            return False, f"{rel} shrank to {size} bytes (floor {floor})"
        ceiling = runner.get("max_bytes")
        if ceiling is not None and size > int(ceiling):
            return False, f"{rel} grew to {size} bytes (ceiling {ceiling})"
        return True, f"{size} bytes"
    if kind == "tree-guard":
        import fnmatch
        root = repo / runner.get("root", ".")
        allow = runner.get("allow", [])
        fixture_paths = set(manifest)
        strays = []
        entries = sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                         if p.is_symlink() or p.is_file())      # a planted symlink is a new entry too
        for rel in entries:
            full = (PurePosixPath(runner.get("root", ".")) / rel).as_posix() if runner.get("root", ".") != "." else rel
            if full in fixture_paths:
                continue
            if any(fnmatch.fnmatch(full, pattern) for pattern in allow):
                continue
            strays.append(full)
        limit = int(runner.get("max_new_files", 0))
        if len(strays) > limit:
            return False, "unexpected files: " + ", ".join(strays[:8])
        return True, f"{len(strays)} new file(s) within the allowance"
    if kind == "shadow-guard":
        root = repo / runner.get("root", ".")
        names = {str(n) for n in (runner.get("modules") or [])}
        fixture_paths = set(manifest)
        # A top-level entry that already exists in the fixture (a package the
        # tests import) may gain files; only a NEW top-level entry can shadow.
        existing_tops = {path.split("/")[0] for path in fixture_paths}
        hits = []
        for p in sorted(root.rglob("*")):
            if not (p.is_symlink() or p.is_file()):
                continue
            rel = p.relative_to(root).as_posix()
            full = (PurePosixPath(runner.get("root", ".")) / rel).as_posix() if runner.get("root", ".") != "." else rel
            if full in fixture_paths:
                continue
            parts = rel.split("/")
            top, leaf = parts[0], parts[-1]
            stem = top[:-3] if top.endswith(".py") else top
            new_top = top not in existing_tops
            # The module a NEW entry would define, at any depth: `x/y.py` -> x/y,
            # `x/y/__init__.py` -> x/y. A planted definition whose path or bare
            # name matches an import the tests make shadows it wherever the test
            # runner's path starts (reviewer, 2026-09-29: tests importing from a
            # subpackage were not covered by the top-level rule).
            if rel.endswith("/__init__.py"):
                module = rel[:-len("/__init__.py")]
            elif rel.endswith(".py"):
                module = rel[:-3]
            else:
                module = None
            module_hit = module is not None and (module in names or module.rsplit("/", 1)[-1] in names)
            # An added conftest.py is removed from every copy a check runs on
            # (`exclude_added_conftests`), so it can hook nothing.
            if leaf == "conftest.py" and not module_hit and full not in names:
                continue
            if (leaf in SHADOW_FILES or leaf.endswith(".pth") or full in names or module_hit
                    or (new_top and (stem in names or top in names or stem in SHADOW_STDLIB))):
                hits.append(full)
        if hits:
            return False, "planted entries would shadow the module under test or hook the harness: " + ", ".join(hits[:8])
        return True, "no shadowing entry"
    raise ValueError(f"unknown structural runner {kind!r}")


# ---------------------------------------------------------------------------
# Command runners
# ---------------------------------------------------------------------------

def _suite(path: str) -> str:
    """A path inside /tests/suites, kept inside it."""
    base = HERE / "suites"
    candidate = (base / path).resolve()
    if not str(candidate).startswith(str(base.resolve())):
        raise ValueError(f"suite path escapes /tests/suites: {path}")
    return str(candidate)



#: Names the test framework rebinds during an honest session (found by running
#: the reference under the full-module snapshot); everything else that changes
#: between collection and session end is tampering.
ALLOWED_REBINDS = frozenset({
    "_pytest.config.Config._getini_unknown_type",   # pytest 8.3 rebinds it while reading ini options
    "warnings._showwarnmsg_impl",                   # pytest's warnings capture patches it per test
})

#: Read one JSON line from stdin -- {"nodes", "args", "report", "nonce", "suites"} --
#: then run pytest and write a JUnit report whose suite name is the nonce.
#:
#: The tests import the agent's code into THIS process, so anything this
#: process keeps in an obvious place is the agent's to read. Until 2026-09-30
#: the line read from stdin stayed in `__main__.cfg`, and a twenty-line block
#: prepended to a fixture module -- read `__main__.cfg`, write a report with
#: the nonce and one passing case per selected node, `os._exit(0)` -- scored
#: 1.0 on a validated task with no fix (reviewer finding S1, reproduced). Now:
#:   - the configuration is consumed inside a function and deleted; nothing
#:     of it stays in `__main__`, `sys.argv`, the environment or stdin (which
#:     is closed and replaced by /dev/null once read);
#:   - the report path and the nonce live only in the closure of a reporter
#:     plugin built by a helper whose frame is gone before any test module is
#:     imported, so a walk of the live frames' locals finds neither;
#:   - pytest's own junitxml plugin is disabled and the report is written by
#:     that closure at session finish, so the nonce is not in pytest's Config
#:     either (`--junitxml`, `junit_suite_name`).
#: What remains reachable is the plugin object through the plugin manager or
#: the garbage collector, and from it the closure cells. That residual is
#: exercised by no control and is declared in every bundle's reward-hacking
#: note; the `report_forge` control proves the routes above are closed.
PYTEST_BOOTSTRAP = (
    "import json, os, sys\n"
    "import pytest\n"
    "def _fsig(value):\n"
    "    # a function is the same only if its object, code, defaults and closure cells are\n"
    "    inner = getattr(value, '__func__', None) or getattr(value, 'fget', None) or value\n"
    "    code = getattr(inner, '__code__', None)\n"
    "    cells = tuple(id(c.cell_contents) for c in (getattr(inner, '__closure__', None) or ()) if c is not None)\n"
    "    return (id(value), id(inner), id(code), id(getattr(inner, '__defaults__', None)), id(getattr(inner, '__kwdefaults__', None)), cells)\n"
    "# names the framework itself rebinds during a session; found by running the reference\n"
    "ALLOWED_REBINDS = set(" + repr(sorted(ALLOWED_REBINDS)) + ")\n"
    "def _module_ok(name, mod, private):\n"
    "    file = getattr(mod, '__file__', None) or ''\n"
    "    if file:\n"
    "        real = os.path.realpath(file)\n"
    "        for prefix in private:\n"
    "            if prefix and real.startswith(prefix):\n"
    "                return False\n"
    "    return True\n"
    "def _snapshot(private=()):\n"
    "    # identity of every function, builtin and method in every module imported so far,\n"
    "    # except the hidden suite (fingerprinted separately) and the agent's own tree\n"
    "    import inspect\n"
    "    seen = {}\n"
    "    for name, mod in list(sys.modules.items()):\n"
    "        if mod is None or not _module_ok(name, mod, private):\n"
    "            continue\n"
    "        try:\n"
    "            items = list(vars(mod).items())\n"
    "        except Exception:\n"
    "            continue\n"
    "        for attr, value in items:\n"
    "            if inspect.isfunction(value) or inspect.isbuiltin(value):\n"
    "                seen[name + '.' + attr] = _fsig(value)\n"
    "            elif inspect.isclass(value) and getattr(value, '__module__', None) == name:\n"
    "                try:\n"
    "                    members = list(vars(value).items())\n"
    "                except Exception:\n"
    "                    continue\n"
    "                for m_attr, m_value in members:\n"
    "                    if inspect.isfunction(m_value) or isinstance(m_value, (classmethod, staticmethod, property)):\n"
    "                        seen[name + '.' + attr + '.' + m_attr] = _fsig(m_value)\n"
    "    return seen\n"
    "def _fingerprint(value, depth=0):\n"
    "    # a structural fingerprint of a suite module's data; functions and classes by identity\n"
    "    import inspect\n"
    "    if depth > 6:\n"
    "        return '...'\n"
    "    if value is None or isinstance(value, (bool, int, float, str, bytes)):\n"
    "        return repr(value)\n"
    "    if isinstance(value, (list, tuple)):\n"
    "        return '[' + ','.join(_fingerprint(v, depth + 1) for v in value) + ']'\n"
    "    if isinstance(value, (set, frozenset)):\n"
    "        return '{' + ','.join(sorted(_fingerprint(v, depth + 1) for v in value)) + '}'\n"
    "    if isinstance(value, dict):\n"
    "        return '{' + ','.join(_fingerprint(k, depth + 1) + ':' + _fingerprint(v, depth + 1) for k, v in value.items()) + '}'\n"
    "    if inspect.isfunction(value) or inspect.isclass(value) or inspect.ismodule(value):\n"
    "        code = getattr(value, '__code__', None)\n"
    "        inner = ''\n"
    "        if inspect.isclass(value):\n"
    "            inner = ','.join(a + '=' + str(id(v)) for a, v in sorted(vars(value).items()) if inspect.isfunction(v))\n"
    "        return '<%s %s %s %s>' % (type(value).__name__, id(value), id(code) if code else '', inner)\n"
    "    return '<%s %s>' % (type(value).__name__, id(value))\n"
    "def _suite_state(modules):\n"
    "    # functions and classes only: a suite legitimately fills its own caches and\n"
    "    # lists while it runs (test_ctrl_multipliers, 2026-09-30); replacing its helpers is\n"
    "    # the tampering, and data is protected by the detachment\n"
    "    import inspect\n"
    "    return {name: _fingerprint({k: v for k, v in vars(mod).items()\n"
    "                                if not k.startswith('__') and (inspect.isfunction(v) or inspect.isclass(v))})\n"
    "            for name, mod in modules.items()}\n"
    "def _reporter(report_path, nonce, suites_dir):\n"
    "    outcomes = {}\n"
    "    private = (os.path.realpath(suites_dir) + os.sep, os.path.realpath(os.environ.get('ARENA_REPO', '/app/repo')) + os.sep)\n"
    "    baseline = _snapshot(private)\n"
    "    held = {}\n"
    "    suite_baseline = {}\n"
    "    def scrub():\n"
    "        for key in ('PYTEST_CURRENT_TEST',):\n"
    "            os.environ.pop(key, None)\n"
    "    class _Arena:\n"
    "        @pytest.fixture(name='tmp_path')\n"
    "        def anonymous_tmp_path(self, tmp_path_factory):\n"
    "            # Public subprocess arguments must not disclose the private case name.\n"
    "            import tempfile\n"
    "            from pathlib import Path\n"
    "            return Path(tempfile.mkdtemp(prefix='work-', dir=str(tmp_path_factory.getbasetemp())))\n"
    "        @pytest.hookimpl(trylast=True)\n"
    "        def pytest_collection_finish(self, session):\n"
    "            # DETACH the hidden suite from the import system: after collection no\n"
    "            # ordinary import can reach a suite module (a reviewer emptied a suite's\n"
    "            # case lists from the fixture module, 2026-09-30). pytest keeps its own\n"
    "            # references, so the collected tests still run.\n"
    "            import importlib\n"
    "            prefix = os.path.realpath(suites_dir) + os.sep\n"
    "            for name, mod in list(sys.modules.items()):\n"
    "                file = getattr(mod, '__file__', None) or ''\n"
    "                if file and os.path.realpath(file).startswith(prefix):\n"
    "                    held[name] = mod\n"
    "                    del sys.modules[name]\n"
    "            sys.path[:] = [p for p in sys.path if os.path.realpath(p or '.') != os.path.realpath(suites_dir)]\n"
    "            sys.path_importer_cache.clear()\n"
    "            importlib.invalidate_caches()\n"
    "            suite_baseline.update(_suite_state(held))\n"
    "            # modules the suite imported at collection join the baseline now\n"
    "            for k, v in _snapshot(private).items():\n"
    "                baseline.setdefault(k, v)\n"
    "        @pytest.hookimpl(tryfirst=True)\n"
    "        def pytest_runtest_setup(self, item):\n"
    "            scrub()\n"
    "        @pytest.hookimpl(tryfirst=True)\n"
    "        def pytest_runtest_call(self, item):\n"
    "            scrub()\n"
    "        @pytest.hookimpl(tryfirst=True)\n"
    "        def pytest_runtest_teardown(self, item):\n"
    "            scrub()\n"
    "        def pytest_runtest_logreport(self, report):\n"
    "            key = report.nodeid\n"
    "            if report.failed:\n"
    "                outcomes[key] = 'failed'\n"
    "            elif report.skipped:\n"
    "                outcomes.setdefault(key, 'skipped')\n"
    "            elif report.when == 'call':\n"
    "                outcomes.setdefault(key, 'passed')\n"
    "        @pytest.hookimpl(tryfirst=True)\n"
    "        def pytest_sessionfinish(self, session, exitstatus):\n"
    "            from xml.sax.saxutils import quoteattr\n"
    "            final = _snapshot(private)\n"
    "            mutated = sorted(k for k, v in baseline.items() if (k not in final or final[k] != v) and k not in ALLOWED_REBINDS)\n"
    "            now = _suite_state(held)\n"
    "            mutated += sorted('suite:' + k for k, v in suite_baseline.items() if now.get(k) != v)\n"
    "            suite_name = nonce\n"
    "            if mutated:\n"
    "                suite_name = 'tampered'\n"
    "                sys.stderr.write('arena: the test framework was mutated before the session ended: '\n"
    "                                 + ', '.join(mutated[:8]) + '\\n')\n"
    "            cases = []\n"
    "            for nodeid, outcome in outcomes.items():\n"
    "                path, _, rest = nodeid.partition('::')\n"
    "                mod = path.rsplit('/', 1)[-1]\n"
    "                mod = mod[:-3] if mod.endswith('.py') else mod\n"
    "                name = rest.split('::')[-1] if rest else mod\n"
    "                inner = '' if outcome == 'passed' else ('<skipped/>' if outcome == 'skipped' else '<failure/>')\n"
    "                cases.append('<testcase classname=%s name=%s>%s</testcase>' % (quoteattr(mod), quoteattr(name), inner))\n"
    "            body = ('<?xml version=\"1.0\" encoding=\"utf-8\"?><testsuites><testsuite name=%s tests=\"%d\">%s'\n"
    "                    '</testsuite></testsuites>' % (quoteattr(suite_name), len(cases), ''.join(cases)))\n"
    "            with open(report_path, 'w') as fh:\n"
    "                fh.write(body)\n"
    "    return _Arena()\n"
    "def _prepare():\n"
    "    cfg = json.loads(sys.stdin.readline())\n"
    "    try:\n"
    "        sys.stdin.close()\n"
    "    except Exception:\n"
    "        pass\n"
    "    sys.stdin = open('/dev/null')\n"
    "    sys.argv = ['pytest']\n"
    "    return (['-q', '-p', 'no:cacheprovider', '-p', 'no:junitxml', '--rootdir', cfg['suites'],\n"
    "             '--confcutdir', cfg['suites'], '-o', 'addopts=', *cfg['args'], *cfg['nodes']],\n"
    "            [_reporter(cfg['report'], cfg['nonce'], cfg['suites'])])\n"
    "sys.exit(pytest.main(*_prepare()))\n")

#: Same contract for unittest: the loader runs the selected names and the
#: result -- every test id run, failures, errors, skips, the nonce -- is
#: written to the report path from stdin by a closure built before any test
#: module is imported; the configuration itself is deleted first.
UNITTEST_BOOTSTRAP = (
    "import json, sys, unittest\n"
    "def _fsig(value):\n"
    "    # a function is the same only if its object, code, defaults and closure cells are\n"
    "    inner = getattr(value, '__func__', None) or getattr(value, 'fget', None) or value\n"
    "    code = getattr(inner, '__code__', None)\n"
    "    cells = tuple(id(c.cell_contents) for c in (getattr(inner, '__closure__', None) or ()) if c is not None)\n"
    "    return (id(value), id(inner), id(code), id(getattr(inner, '__defaults__', None)), id(getattr(inner, '__kwdefaults__', None)), cells)\n"
    "import os\n"
    "ALLOWED_REBINDS = set(" + repr(sorted(ALLOWED_REBINDS)) + ")\n"
    "def _module_ok(name, mod, private):\n"
    "    file = getattr(mod, '__file__', None) or ''\n"
    "    if file:\n"
    "        real = os.path.realpath(file)\n"
    "        for prefix in private:\n"
    "            if prefix and real.startswith(prefix):\n"
    "                return False\n"
    "    return True\n"
    "def _snapshot(private=()):\n"
    "    import inspect\n"
    "    seen = {}\n"
    "    for name, mod in list(sys.modules.items()):\n"
    "        if mod is None or not _module_ok(name, mod, private):\n"
    "            continue\n"
    "        try:\n"
    "            items = list(vars(mod).items())\n"
    "        except Exception:\n"
    "            continue\n"
    "        for attr, value in items:\n"
    "            if inspect.isfunction(value) or inspect.isbuiltin(value):\n"
    "                seen[name + '.' + attr] = _fsig(value)\n"
    "            elif inspect.isclass(value) and getattr(value, '__module__', None) == name:\n"
    "                try:\n"
    "                    members = list(vars(value).items())\n"
    "                except Exception:\n"
    "                    continue\n"
    "                for m_attr, m_value in members:\n"
    "                    if inspect.isfunction(m_value) or isinstance(m_value, (classmethod, staticmethod, property)):\n"
    "                        seen[name + '.' + attr + '.' + m_attr] = _fsig(m_value)\n"
    "    return seen\n"
    "def _fingerprint(value, depth=0):\n"
    "    import inspect\n"
    "    if depth > 6:\n"
    "        return '...'\n"
    "    if value is None or isinstance(value, (bool, int, float, str, bytes)):\n"
    "        return repr(value)\n"
    "    if isinstance(value, (list, tuple)):\n"
    "        return '[' + ','.join(_fingerprint(v, depth + 1) for v in value) + ']'\n"
    "    if isinstance(value, (set, frozenset)):\n"
    "        return '{' + ','.join(sorted(_fingerprint(v, depth + 1) for v in value)) + '}'\n"
    "    if isinstance(value, dict):\n"
    "        return '{' + ','.join(_fingerprint(k, depth + 1) + ':' + _fingerprint(v, depth + 1) for k, v in value.items()) + '}'\n"
    "    if inspect.isfunction(value) or inspect.isclass(value) or inspect.ismodule(value):\n"
    "        code = getattr(value, '__code__', None)\n"
    "        inner = ''\n"
    "        if inspect.isclass(value):\n"
    "            inner = ','.join(a + '=' + str(id(v)) for a, v in sorted(vars(value).items()) if inspect.isfunction(v))\n"
    "        return '<%s %s %s %s>' % (type(value).__name__, id(value), id(code) if code else '', inner)\n"
    "    return '<%s %s>' % (type(value).__name__, id(value))\n"
    "def _suite_state(modules):\n"
    "    # functions and classes only: a suite legitimately fills its own caches and\n"
    "    # lists while it runs (test_ctrl_multipliers, 2026-09-30); replacing its helpers is\n"
    "    # the tampering, and data is protected by the detachment\n"
    "    import inspect\n"
    "    return {name: _fingerprint({k: v for k, v in vars(mod).items()\n"
    "                                if not k.startswith('__') and (inspect.isfunction(v) or inspect.isclass(v))})\n"
    "            for name, mod in modules.items()}\n"
    "def _finisher(report, nonce, suites):\n"
    "    private = (os.path.realpath(suites) + os.sep, os.path.realpath(os.environ.get('ARENA_REPO', '/app/repo')) + os.sep)\n"
    "    baseline = _snapshot(private)\n"
    "    held = {}\n"
    "    suite_baseline = {}\n"
    "    def hold(modules):\n"
    "        held.update(modules)\n"
    "        suite_baseline.update(_suite_state(held))\n"
    "        for k, v in _snapshot(private).items():\n"
    "            baseline.setdefault(k, v)\n"
    "    def finish(ids, result):\n"
    "        final = _snapshot(private)\n"
    "        mutated = sorted(k for k, v in baseline.items() if (k not in final or final[k] != v) and k not in ALLOWED_REBINDS)\n"
    "        now = _suite_state(held)\n"
    "        mutated += sorted('suite:' + k for k, v in suite_baseline.items() if now.get(k) != v)\n"
    "        stamp = nonce\n"
    "        if mutated:\n"
    "            stamp = 'tampered'\n"
    "            sys.stderr.write('arena: the test framework was mutated before the run ended: ' + ', '.join(mutated[:8]) + '\\n')\n"
    "        bad = [t.id() for t, _ in result.failures + result.errors] + [t.id() for t, _ in result.skipped]\n"
    "        json.dump({'nonce': stamp, 'ran': ids, 'testsRun': result.testsRun,\n"
    "                   'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),\n"
    "                   'bad': bad}, open(report, 'w'))\n"
    "        return 0 if result.wasSuccessful() else 1\n"
    "    finish.hold = hold\n"
    "    return finish\n"
    "def _boot():\n"
    "    cfg = json.loads(sys.stdin.readline())\n"
    "    try:\n"
    "        sys.stdin.close()\n"
    "    except Exception:\n"
    "        pass\n"
    "    sys.stdin = open('/dev/null')\n"
    "    sys.argv = ['unittest']\n"
    "    finish = _finisher(cfg['report'], cfg['nonce'], cfg['suites'])\n"
    "    suites, nodes = cfg['suites'], list(cfg['nodes'])\n"
    "    del cfg\n"
    "    sys.path.insert(0, suites)\n"
    "    loader = unittest.defaultTestLoader\n"
    "    suite = loader.loadTestsFromNames(nodes) if nodes else loader.discover(suites, pattern='test*.py')\n"
    "    ids = []\n"
    "    def walk(s):\n"
    "        for item in s:\n"
    "            if isinstance(item, unittest.TestSuite): walk(item)\n"
    "            else: ids.append(item.id())\n"
    "    walk(suite)\n"
    "    # detach the loaded suite from the import system (see the pytest bootstrap)\n"
    "    import importlib, os\n"
    "    prefix = os.path.realpath(suites) + os.sep\n"
    "    held = {}\n"
    "    for name, mod in list(sys.modules.items()):\n"
    "        file = getattr(mod, '__file__', None) or ''\n"
    "        if file and os.path.realpath(file).startswith(prefix):\n"
    "            held[name] = mod\n"
    "            del sys.modules[name]\n"
    "    sys.path[:] = [p for p in sys.path if os.path.realpath(p or '.') != os.path.realpath(suites)]\n"
    "    sys.path_importer_cache.clear()\n"
    "    importlib.invalidate_caches()\n"
    "    finish.hold(held)\n"
    "    result = unittest.TextTestRunner(verbosity=2).run(suite)\n"
    "    return finish(ids, result)\n"
    "sys.exit(_boot())\n")


def command_for(runner: dict, tree: Path, report_dir: Path | None = None) -> tuple[list[str] | str, bool]:
    """(argv or shell string, is_shell) for a runner type.

    With `report_dir`, every test runner is asked for a machine-readable result
    (JUnit XML, JSON, TAP) so the grader can COUNT completed tests instead of
    trusting an exit status: a process that ends early with status 0 -- an
    `os._exit(0)` planted in a module the tests import -- has run nothing.
    """
    kind = runner["type"]
    reports = report_dir is not None
    if kind == "command":
        if "argv" in runner:
            return list(runner["argv"]), False
        return str(runner["shell"]), True
    if kind == "pytest":
        # Isolated interpreter (-I: no PYTHONPATH, no user site, no cwd on
        # sys.path) run from /tests/suites, never from the agent's tree: a
        # `sitecustomize.py` or `conftest.py` planted in the repository must
        # not reach the grader's process. Tests import the repository through
        # ARENA_REPO explicitly, after startup. The selection, the report path
        # and the report nonce reach pytest through a bootstrap read from
        # stdin, so nothing the agent's code can read from argv or the
        # environment names the file the grader will trust.
        select = runner.get("select") or []
        nodes = []
        for item in select:
            file_part, _, test_part = item.partition("::")
            node = _suite(file_part)
            nodes.append(node + ("::" + test_part if test_part else ""))
        if not reports:
            return [sys.executable, "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                    "--rootdir", str(HERE / "suites"), "--confcutdir", str(HERE / "suites"),
                    "-o", "addopts=", *runner.get("args", []), *nodes], False
        return [sys.executable, "-I", "-c", PYTEST_BOOTSTRAP], False
    if kind == "unittest":
        # `-I` keeps the working directory off sys.path, so the suites
        # directory is added explicitly -- and only it: the repository is
        # reached through ARENA_REPO by the tests themselves.
        return [sys.executable, "-I", "-c", UNITTEST_BOOTSTRAP], False
    if kind == "node-test":
        junit = ["--test-reporter=junit", f"--test-reporter-destination={report_dir / 'junit.xml'}"] if reports else []
        return ["node", "--test", *junit, *[_suite(p) for p in runner.get("select", [])]], False
    if kind in ("jest", "vitest", "mocha"):
        binary = str(tree / "node_modules" / ".bin" / kind)
        args = list(runner.get("args", []))
        if kind == "jest":
            args = ["--ci", "--runInBand", "--rootDir", str(tree), *args]
            if reports:
                args += ["--json", "--outputFile", str(report_dir / "jest.json")]
        elif kind == "vitest":
            args = ["run", "--root", str(tree), *args]
            if reports:
                args += ["--reporter=json", "--outputFile", str(report_dir / "vitest.json")]
        elif reports:
            args += ["--reporter", "json"]
        return [binary, *args, *[_suite(p) for p in runner.get("select", [])]], False
    if kind == "playwright":
        binary = shutil.which("npx") or "npx"
        reporter = ["--reporter=json"] if reports else []
        # /tests is read-only in the verifier; test artefacts go to the private report directory.
        output = ["--output", str(report_dir / "playwright-output")] if reports else []
        return [binary, "playwright", "test", "-c", _suite(runner.get("config", "playwright.config.js")),
                *reporter, *output, *[_suite(p) for p in runner.get("select", [])]], False
    if kind == "gradle":
        return ["gradle", "--offline", "-q", "test", *[a for t in runner.get("select", []) for a in ("--tests", t)]], False
    if kind == "maven":
        tests = ",".join(runner.get("select", []))
        return ["mvn", "-o", "-q", "test", *(["-Dtest=" + tests] if tests else [])], False
    if kind == "cargo":
        return ["cargo", "test", "--offline", "-q", *runner.get("select", [])], False
    if kind == "go":
        flag = ["-json"] if reports else []
        return ["go", "test", *flag, *runner.get("args", []), *(runner.get("select") or ["./..."])], False
    raise ValueError(f"unknown runner type {kind!r}")


# ---------------------------------------------------------------------------
# Completed-test accounting
# ---------------------------------------------------------------------------
# Exit status is not evidence. Every test runner reports how many tests it
# completed, and a check passes only when at least one test ran, none failed,
# errored or was skipped. Reviewer finding (2026-09-27): an `os._exit(0)` in
# an existing module the hidden tests import ended each pytest process before
# any assertion, with status 0.

def _junit_counts(paths: list[Path]) -> dict | None:
    import xml.etree.ElementTree as ET
    total = {"passed": 0, "failed": 0, "skipped": 0}
    seen = False
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        for suite in suites:
            seen = True
            tests = int(suite.get("tests", 0) or 0)
            failed = int(suite.get("failures", 0) or 0) + int(suite.get("errors", 0) or 0)
            skipped = int(suite.get("skipped", 0) or 0)
            total["failed"] += failed
            total["skipped"] += skipped
            total["passed"] += max(tests - failed - skipped, 0)
    return total if seen else None


def _jest_counts(path: Path) -> dict | None:
    try:
        doc = _load_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or "numTotalTests" not in doc:
        return None
    return {"passed": int(doc.get("numPassedTests", 0)),
            "failed": int(doc.get("numFailedTests", 0)) + int(doc.get("numRuntimeErrorTestSuites", 0)),
            "skipped": int(doc.get("numPendingTests", 0)) + int(doc.get("numTodoTests", 0))}


def _last_json_object(text: str) -> dict | None:
    start = text.find("{")
    while start != -1:
        try:
            doc = json.loads(text[start:])
            return doc if isinstance(doc, dict) else None
        except ValueError:
            start = text.find("{", start + 1)
    return None


def _pytest_report_counts(path: Path, nonce: str, select: list[str]) -> dict | None:
    """Counts from pytest's JUnit report, only if it is the report this run asked for.

    The suite name must be this run's nonce and every selected node must
    appear as a test case: a report written by imported agent code before an
    early exit carries neither. The document must hold exactly ONE suite: the
    bootstrap writes one, and a report carrying a suite per candidate string
    found in memory (a shotgun at the nonce) is refused whole.
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if len(suites) != 1:
        return None
    suites = [s for s in suites if s.get("name") == nonce]
    if not suites:
        return None
    cases = [c for s in suites for c in s.iter("testcase")]
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    seen: list[tuple[str, str]] = []
    for case in cases:
        classname, name = case.get("classname", ""), case.get("name", "")
        seen.append((classname, name))
        if case.find("failure") is not None or case.find("error") is not None:
            counts["failed"] += 1
        elif case.find("skipped") is not None:
            counts["skipped"] += 1
        else:
            counts["passed"] += 1
    for item in select:
        file_part, _, test_part = item.partition("::")
        module = Path(file_part).stem
        want_name = test_part.split("::")[-1].split("[")[0] if test_part else None
        matched = [c for c in seen if c[0].split(".")[-1] == module or c[0].endswith(module)]
        if want_name:
            matched = [c for c in matched if c[1].split("[")[0] == want_name]
        if not matched:
            return None                        # the report does not cover the selection
    return counts


def _unittest_report_counts(path: Path, nonce: str, select: list[str]) -> dict | None:
    try:
        doc = _load_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("nonce") != nonce:
        return None
    ran = [str(x) for x in doc.get("ran") or []]
    for name in select:
        if not any(r == name or r.startswith(name + ".") for r in ran):
            return None
    failed = int(doc.get("failures", 0)) + int(doc.get("errors", 0))
    skipped = int(doc.get("skipped", 0))
    return {"passed": max(int(doc.get("testsRun", 0)) - failed - skipped, 0), "failed": failed, "skipped": skipped}


def test_counts(kind: str, stdout: str, stderr: str, report_dir: Path, tree: Path,
                nonce: str = "", select: list[str] | None = None) -> dict | None:
    """(passed, failed, skipped) the runner itself reported, or None when it reported nothing."""
    select = list(select or [])
    if kind == "pytest":
        return _pytest_report_counts(report_dir / "junit.xml", nonce, select)
    if kind == "unittest":
        return _unittest_report_counts(report_dir / "unittest.json", nonce, select)
    if kind == "node-test":
        counts = _junit_counts([report_dir / "junit.xml"])
        if counts is None:
            return None
        # Node's JUnit reporter names one suite per file; every selected file
        # must be among them.
        import xml.etree.ElementTree as ET
        try:
            names = {s.get("name", "") for s in ET.parse(report_dir / "junit.xml").getroot().iter("testsuite")}
        except (OSError, ET.ParseError):
            return None
        for item in select:
            if not any(n.endswith(Path(item).name) for n in names):
                return None
        return counts
    if kind == "jest":
        return _jest_counts(report_dir / "jest.json")
    if kind == "vitest":
        return _jest_counts(report_dir / "vitest.json")
    if kind == "mocha":
        doc = _last_json_object(stdout)
        stats = (doc or {}).get("stats")
        if not isinstance(stats, dict):
            return None
        return {"passed": int(stats.get("passes", 0)), "failed": int(stats.get("failures", 0)),
                "skipped": int(stats.get("pending", 0))}
    if kind == "playwright":
        doc = _last_json_object(stdout)
        stats = (doc or {}).get("stats")
        if not isinstance(stats, dict):
            return None
        return {"passed": int(stats.get("expected", 0)),
                "failed": int(stats.get("unexpected", 0)) + int(stats.get("flaky", 0)),
                "skipped": int(stats.get("skipped", 0))}
    if kind == "go":
        counts = {"passed": 0, "failed": 0, "skipped": 0}
        seen = False
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or not event.get("Test"):
                continue
            action = event.get("Action")
            if action == "pass":
                counts["passed"] += 1; seen = True
            elif action == "fail":
                counts["failed"] += 1; seen = True
            elif action == "skip":
                counts["skipped"] += 1; seen = True
        return counts if seen else None
    if kind == "cargo":
        counts = {"passed": 0, "failed": 0, "skipped": 0}
        seen = False
        for m in re.finditer(r"test result: (\w+)\. (\d+) passed; (\d+) failed; (\d+) ignored", stdout + stderr):
            seen = True
            counts["passed"] += int(m.group(2)); counts["failed"] += int(m.group(3)); counts["skipped"] += int(m.group(4))
        return counts if seen else None
    if kind in ("gradle", "maven"):
        pattern = "build/test-results/**/*.xml" if kind == "gradle" else "target/surefire-reports/TEST-*.xml"
        return _junit_counts(sorted(tree.glob(pattern)))
    return None


COUNTED_RUNNERS = ("pytest", "unittest", "node-test", "jest", "vitest", "mocha", "playwright",
                   "go", "cargo", "gradle", "maven")


#: The unprivileged user the verifier image creates for test processes. When
#: the grader runs as root (Harbor's verifier does) every runner process is
#: started as this user, after the per-check tree, its HOME and the private
#: report directory have been handed to it; /tests stays root-owned and
#: read-only. Code a hidden test imports then cannot rewrite a later test.
GRADER_USER = "grader"


def _grader_ids() -> tuple[int, int] | None:
    if os.geteuid() != 0:
        return None
    try:
        import pwd
        entry = pwd.getpwnam(GRADER_USER)
    except (ImportError, KeyError):
        return None
    return entry.pw_uid, entry.pw_gid


def _chown_tree(root: Path, ids: tuple[int, int]) -> None:
    uid, gid = ids
    for dirpath, dirnames, filenames in os.walk(root):
        os.lchown(dirpath, uid, gid)
        # Files, and symlinked directories (os.walk never descends into them).
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            try:
                os.lchown(os.path.join(dirpath, name), uid, gid)
            except OSError:
                pass


def _drop_privileges(ids: tuple[int, int] | None):
    """A `preexec_fn` that becomes the grader user, or None when already unprivileged."""
    if ids is None:
        return None
    uid, gid = ids

    def become():
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    return become



#: Reviewer finding S1 (2026-09-30, twice): pytest and unittest import the
#: agent's code into the grader's own interpreter, where a frame walk, a scan of
#: the garbage collector or the plugin manager reaches the reporter that writes
#: the run's report, and a determined module could fill it in and exit 0. No
#: in-process design closes that. What can be closed is the ROUTE: a file the
#: agent wrote or changed that reaches for the interpreter's internals is
#: refused before the suite runs, and the check fails closed with that reason.
#: Files the fixture already held (hash in the manifest) are never scanned, so
#: a repository's own uses of these names cannot fail an honest fix.
INTROSPECTION_TOKENS = (
    r"_getframe", r"currentframe\(", r"inspect\.stack\(", r"\.f_back\b", r"\.f_locals\b", r"\.f_globals\b",
    r"gc\.get_objects", r"gc\.get_referrers", r"gc\.get_referents", r"__closure__", r"cell_contents",
    r"co_freevars", r"pytest_sessionfinish", r"pytest_runtest_logreport", r"_Arena\b",
    r"import\s+__main__", r"sys\.modules\[\s*['\"]__main__", r"\bsettrace\(", r"\bsetprofile\(",
    r"\bimport\s+ctypes\b", r"\bfrom\s+ctypes\b", r"os\._exit\(",
    r"getattr\(\s*sys\s*,", r"getattr\(\s*gc\s*,", r"getattr\(\s*inspect\s*,", r"vars\(\s*sys\s*\)",
    r"sys\.__dict__", r"__import__\(", r"import_module\(", r"(?<![\w.])exec\(", r"(?<![\w.])eval\(",
    r"(?<![\w.])compile\(", r"_pytest\b",
    r"\bimport\s+builtins\b", r"__builtins__", r"sys\.meta_path", r"sys\.path_hooks", r"sys\.excepthook",
    r"TestCase\.\w+\s*=", r"sys\.modules\b",
    r"[Pp]lugin[Mm]anager", r"_hookexec", r"__code__", r"__globals__", r"__defaults__", r"__kwdefaults__",
    r"\bFunctionType\b", r"\bCodeType\b", r"\bpluggy\b",
    r"\bimport\s+(conftest|test_\w+)\b", r"\bfrom\s+(conftest|test_\w+)\s+import\b",
)
_INTROSPECTION = re.compile("|".join(INTROSPECTION_TOKENS))

#: Ways Python source gets loaded that the static import walk in `reachable_python` cannot follow.
#: When any reachable file -- fixture or added -- does one of these, every added file counts as
#: reachable and the scan is exactly as strict as it always was.
_DYNAMIC_LOAD = re.compile(r"\brunpy\b|spec_from_file_location|SourceFileLoader|SourcelessFileLoader|"
                           r"importlib\.machinery|importlib\.util|\bzipimport\b|\bpkgutil\b|load_module\(|"
                           r"exec_module\(|import_module\(|__import__\(|(?<![\w.])exec\(|(?<![\w.])eval\(|"
                           r"(?<![\w.])compile\(")
#: In an UNCHANGED fixture file only loading BY NAME OR PATH can reach an agent-written file (a plugin list the
#: agent may edit), or exec/eval/compile of a file's contents; eval() over strings is the repository's own
#: business (python-verification-library's fixture evaluates expressions, 2026-10-09: the broad rule restored
#: the full scan and re-refused the agent's unreachable verify_methods.py). Changed or added reachable files keep
#: the broad rule -- and their own eval/exec/compile is refused by the scan anyway.
_FIXTURE_DYNAMIC_LOAD = re.compile(r"\brunpy\b|spec_from_file_location|SourceFileLoader|SourcelessFileLoader|"
                                   r"\bzipimport\b|\bpkgutil\b|load_module\(|exec_module\(|import_module\(|find_spec\(|"
                                   r"__import__\(|(?<![\w.])(?:exec|eval|compile)\([^\n]*(?:open\(|read_text\(|read_bytes\()")


def _imported_names(text: str) -> set[str]:
    """Dotted names a Python file imports; `from x import y` also yields x.y."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if base:
                names.add(base)
            names.update(f"{base}.{a.name}" if base else a.name for a in node.names if a.name != "*")
    return names


_FILE_LOADERS = ("spec_from_file_location", "SourceFileLoader", "SourcelessFileLoader")


def _constant_loads(text: str, rel: str, tree: Path) -> tuple[list[str], bool]:
    """(the repository files an unchanged module loads by a CONSTANT path, whether it also loads anything else).

    A loader whose location is built only from `__file__`, string constants and module-level names that are
    themselves built that way loads one fixed file -- an import by another name, not a route an agent can steer
    (python-verification-library's plugin_loader.py: spec_from_file_location(name, PLUGIN_DIR / "__init__.py")).
    Anything else (a parameter, a loop variable, a value read from a file) is data-driven: the caller restores the
    full scan."""
    try:
        mod = ast.parse(text)
    except (SyntaxError, ValueError):
        return [], True
    consts: dict[str, ast.AST] = {}
    for node in mod.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            consts[node.targets[0].id] = node.value

    def const(expr, depth=0) -> bool:
        if depth > 8:
            return False
        for sub in ast.walk(expr):
            if isinstance(sub, ast.Name) and sub.id not in ("__file__", "Path", "os", "pathlib"):
                if sub.id not in consts or not const(consts[sub.id], depth + 1):
                    return False
            if isinstance(sub, ast.Call) and not isinstance(sub.func, (ast.Name, ast.Attribute)):
                return False
        return True

    def evaluate(expr, depth=0) -> str:
        """The path tail a constant expression names, joining `/` operands in order: `Path(__file__).parent /
        'plugins' / 'x'` -> 'plugins/x' (the __file__-rooted head is the module's own folder)."""
        if depth > 8:
            return ""
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
            left, right = evaluate(expr.left, depth + 1), evaluate(expr.right, depth + 1)
            return "/".join(x for x in (left, right) if x)
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return expr.value.strip("/")
        if isinstance(expr, ast.Name) and expr.id in consts:
            return evaluate(consts[expr.id], depth + 1)
        if isinstance(expr, ast.Call) and getattr(expr.func, "attr", "") == "joinpath":
            parts = [evaluate(expr.func.value, depth + 1)] + [evaluate(x, depth + 1) for x in expr.args]
            return "/".join(x for x in parts if x)
        return ""

    loads, other = [], False
    for node in ast.walk(mod):
        if not isinstance(node, ast.Call):
            continue
        fname = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if fname in _FILE_LOADERS:
            loc = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg in ("location", "path")), None)
            if loc is None or not const(loc):
                other = True
                continue
            target = evaluate(loc)
            here = (tree / rel).parent
            want = (here / target).resolve() if target else None
            hits = [p.relative_to(tree).as_posix() for p in tree.rglob("*.py") if want and p.resolve() == want]
            # the loaded file's whole package directory: its submodules import each other by the package name
            for h in hits:
                loads += [q.relative_to(tree).as_posix() for q in (tree / h).parent.rglob("*.py")]
            if not hits:
                other = True
    return loads, other


_LOADER_CALLS = ("import_module", "__import__", "run_module", "run_path", "spec_from_file_location",
                 "SourceFileLoader", "load_source", "find_spec")


def _computed_loads(text: str) -> bool:
    """Whether a suite loads a module by a computed name or path (no string literal in the argument that names
    it): then the static walk cannot know the entry points and the full scan applies."""
    try:
        mod = ast.parse(text)
    except (SyntaxError, ValueError):
        return True
    for node in ast.walk(mod):
        if not isinstance(node, ast.Call):
            continue
        fname = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if fname not in _LOADER_CALLS:
            continue
        args = list(node.args) + [k.value for k in node.keywords]
        if fname in ("spec_from_file_location", "SourceFileLoader", "load_source") and len(node.args) > 1:
            args = [node.args[1]]
        if not any(isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value.strip()
                   for a in args for sub in ast.walk(a)):
            return True
    return False


def _module_keys(rel: str) -> list[str]:
    """Every dotted name under which `rel` could be imported, from any directory on the path."""
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return [".".join(parts[k:]) for k in range(len(parts)) if parts[k:]]


def reachable_python(tree: Path, manifest: dict, suites: Path | None,
                     run_names: tuple[str, ...] = ()) -> set[str] | None:
    """Repository Python files a hidden test process can execute: every fixture file, every added
    file the hidden suites import or name, and everything those import, resolved generously (a name
    matches a file of that name in ANY directory, so the set only ever grows). Returns None -- every
    file -- when the suites cannot be read or a reachable file loads source dynamically.

    An added file outside this set cannot run inside the test process: nothing there imports it and
    nothing can load it, so its source cannot reach the reporter (Arena feedback 2026-10-08: an
    agent's own verification script using eval() refused every check of python-verification-library;
    c-019's helper importing the agent's own test file did the same). `run_names` are the modules,
    nodes and paths the check's runner executes directly; a tree file they name is an entry point,
    and so is every file of the suites directory when it lies inside the tree.
    """
    tree = Path(tree)
    if suites is None or not Path(suites).is_dir():
        return None
    files = {p.relative_to(tree).as_posix(): p for p in sorted(tree.rglob("*.py"))
             if p.is_file() and not p.is_symlink()}
    index: dict[str, set[str]] = {}
    for rel in files:
        for key in _module_keys(rel):
            index.setdefault(key, set()).add(rel)
    mentioned, imported = [], set()
    for p in sorted(Path(suites).rglob("*")):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        mentioned.append(text)
        if p.suffix == ".py":
            imported |= _imported_names(text)
            if _computed_loads(text):
                return None
    suite_text = "\n".join(mentioned)
    for entry in run_names:
        head = str(entry).split("::")[0]
        imported.add(head[:-3].replace("/", ".") if head.endswith(".py") else head)
        suite_text += "\n" + str(entry)
    inside = Path(suites).resolve()
    # Entry points: what the suites import, name or select -- fixture and added files alike. A fixture file
    # nothing reaches never runs in the test process, so the agent's edits to it cannot reach the reporter
    # (python-verification-library, Arena 2026-10-08: the user's own oracle script verify_methods.py, edited by the
    # agent, was refused for the eval() it always held, though no hidden test runs it).
    frontier = [rel for rel in files
                if inside == tree.resolve() or inside in (tree / rel).resolve().parents
                or rel in suite_text or Path(rel).name in suite_text or any(k in imported for k in _module_keys(rel))]
    seen: set[str] = set()
    while frontier:
        rel = frontier.pop()
        if rel in seen:
            continue
        seen.add(rel)
        try:
            text = files[rel].read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        unchanged = rel in manifest and _sha256(files[rel]) == (manifest.get(rel) or {}).get("sha256")
        if unchanged and _FIXTURE_DYNAMIC_LOAD.search(text):
            # Constant-path file loads are imports by another name; any other dynamic load restores the full scan.
            loaded, other = _constant_loads(text, rel, tree)
            # module_from_spec / exec_module finish a load whose spec the checked loader call made
            rest = "\n".join(l for l in text.splitlines()
                             if not any(f in l for f in _FILE_LOADERS + ("module_from_spec", "exec_module")))
            if other or _FIXTURE_DYNAMIC_LOAD.search(rest):
                return None
            frontier.extend(loaded)
        elif not unchanged and _DYNAMIC_LOAD.search(text):
            return None
        for name in _imported_names(text):
            parts = name.split(".")
            for k in range(1, len(parts) + 1):
                frontier.extend(index.get(".".join(parts[:k]), ()))
            # generous: a package loaded under another name (a dash in its folder) still resolves by its tail
            frontier.extend(index.get(parts[-1], ()))
    return seen


#: Runners whose test process runs the verifier's own suites; a `command` runner runs our command
#: in the tree, which may execute any file there, so it keeps the full scan.
SUITE_RUNNERS = frozenset({"pytest", "unittest", "node-test", "go-test"})


def scan_problems(tree: Path, manifest: dict, vendored, runner: dict, suites: Path | None) -> list[str]:
    """`introspection_problems` limited, for suite runners, to the Python the hidden tests can reach."""
    names = tuple(str(x) for key in ("select", "modules", "paths", "nodes", "files")
                  for x in (runner.get(key) or []) if isinstance(x, (str, int)))
    reachable = (reachable_python(tree, manifest, suites, names)
                 if runner.get("type") in SUITE_RUNNERS else None)
    return introspection_problems(tree, manifest, vendored, reachable=reachable)


#: The hidden tree, the harness variables and the process's own descriptors
#: are off limits in EVERY language the agent's code may run in: a reviewer
#: showed a changed roles.js reading the expected role list from /tests at
#: grade time (2026-09-30). /tests is unwritable, not unreadable -- the test
#: process must read it -- so a literal reference is refused; an obfuscated
#: one is the declared residual.
#:
#: The hidden-tree paths are matched only where they are ROOTED: at the start
#: of a string, after a quote, a space or another non-path character, or
#: after `..` / `/.`, which climb to it. Arena's review (C-044, 2026-10-05)
#: showed the unrooted `/tests\b` refusing a comment naming the agent's own
#: `neon-city-3d-mobile/tests/renderer.test.cjs`, zeroing two honest webgl
#: episodes (re-graded 0.92); `my-app/tests`, `./tests/` and `src/tests` are
#: the repository's own folders, never the verifier's.
_ROOTED = r"(?:(?<![\w\-.])|(?<=\.\.)|(?<=/\.))"
HIDDEN_TREE_TOKENS = (_ROOTED + r"/tests\b", _ROOTED + r"/app/tests\b", r"/logs/verifier", r"ARENA_",
                      r"/proc/self", r"/dev/fd")
_HIDDEN_TREE = re.compile("|".join(HIDDEN_TREE_TOKENS))
SOURCE_SUFFIXES = (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".php", ".go", ".rs", ".rb", ".java",
                   ".kt", ".cs", ".sh", ".bash", ".zsh", ".swift", ".scala", ".pl", ".lua", ".vue", ".svelte", ".html", ".htm", ".xhtml")


def _html_execution_sources(text: str) -> str:
    """Inline scripts and executable attributes, excluding displayed HTML/data.

    Script types follow the HTML and MIME Sniffing standards. External-script
    documentation is inert; its source address still participates in the scan.
    This is the same literal source guard, not a JavaScript execution proof.
    """
    from html.parser import HTMLParser
    js_types = {"", "module", "application/ecmascript", "application/javascript",
                "application/x-ecmascript", "application/x-javascript", "text/ecmascript",
                "text/javascript", "text/jscript", "text/livescript", "text/x-ecmascript",
                "text/x-javascript", *("text/javascript1." + str(i) for i in range(6))}
    class ExecutionSources(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.sources = []
            self.inline_script = False

        def handle_starttag(self, tag, attrs):
            attributes = {}
            for name, value in attrs:
                attributes.setdefault(name, value)  # HTML keeps the first duplicate.
                if value and (name.startswith("on") or
                        (name in {"href", "src", "action", "formaction", "xlink:href"}
                         and value.lstrip(" \t\n\f\r").lower().startswith("javascript:"))):
                    self.sources.append(value)
            if tag == "script":
                kind = attributes.get("type")
                if kind is None:
                    language = attributes.get("language") or ""
                    kind = "text/" + language if language else ""
                executable = kind.strip(" \t\n\f\r").lower() in js_types
                self.inline_script = executable and "src" not in attributes
                if executable and attributes.get("src"):
                    self.sources.append(attributes["src"])

        def handle_endtag(self, tag):
            if tag == "script":
                self.inline_script = False

        def handle_data(self, data):
            if self.inline_script:
                self.sources.append(data)
    parser = ExecutionSources()
    parser.feed(text)
    parser.close()
    return "\n".join(parser.sources)


def _scoped_syntax(text: str):
    try:
        root = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    found = []

    def walk(node, scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = (*scope, node.name)
        found.append((node, scope))
        for child in ast.iter_child_nodes(node):
            walk(child, scope)

    walk(root, ())
    return found


def _syntax_signature(node, scope):
    return hashlib.sha256(json.dumps([scope, ast.dump(node, include_attributes=False)],
                                    sort_keys=True).encode()).hexdigest()


def fixture_benign_syntax(text: str) -> list[str]:
    """Attest narrow, existing GUI platform syntax, without exempting a file."""
    allowed = []
    for node, scope in _scoped_syntax(text):
        imported = isinstance(node, ast.Import) and len(node.names) == 1 \
            and node.names[0].name == "ctypes" and node.names[0].asname is None
        call = node.value if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) else None
        dpi = call is not None and ast.unparse(call.func) == "ctypes.windll.shcore.SetProcessDpiAwareness" \
            and len(call.args) == 1 and isinstance(call.args[0], ast.Constant) \
            and type(call.args[0].value) is int and call.args[0].value == 2 and not call.keywords
        close = call is not None and ast.unparse(call.func) == "os._exit" \
            and len(scope) >= 2 and scope[-1] in {"closeEvent", "on_close"} and len(call.args) == 1 \
            and isinstance(call.args[0], ast.Constant) and type(call.args[0].value) is int \
            and call.args[0].value == 0 and not call.keywords
        if imported or dpi or close:
            allowed.append(_syntax_signature(node, scope))
    return sorted(set(allowed))


#: Arena's feedback (2026-10-11, R01/R04/R06/R08/R12/R13) found the literal scans refusing correct solutions for
#: TEXT that cannot run: `// ARENA_WIDTH` in a JS comment, `eval(` and `# import builtins` in Python comments, and a
#: bare `compile(` bound by `from re import compile`. The scans below therefore read a CODE VIEW: comments and inert
#: string statements (docstrings) are blanked, byte for byte, before matching. String literals that code USES stay
#: scanned (`process.env["ARENA_STEP"]` is a real reach), and a file that does not lex cleanly is scanned raw --
#: exactly as strictly as before. Blanking never joins or moves code: every byte keeps its offset.

def _python_inert_spans(text: str) -> list[tuple[int, int]]:
    """UTF-8 byte spans of Python comments and bare string statements ([] when the file does not lex and parse)."""
    import io
    import tokenize
    if re.search(r"\r(?!\n)", text):
        return []        # bare CR line ends: the tokenizer and the parser would count lines differently
    try:
        tree = ast.parse(text)
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (SyntaxError, ValueError, tokenize.TokenError):
        return []
    blines = text.encode("utf-8").splitlines(keepends=True)   # \n and \r\n only, as the parser counts
    lines = [b.decode("utf-8") for b in blines]
    starts = [0]
    for line in blines:
        starts.append(starts[-1] + len(line))

    def at_char(row: int, col: int) -> int:
        return starts[row - 1] + len(lines[row - 1][:col].encode("utf-8")) if row <= len(lines) else starts[-1]

    spans = [(at_char(*t.start), at_char(*t.end)) for t in tokens if t.type == tokenize.COMMENT]
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, (str, bytes)):
            v = node.value       # AST columns count UTF-8 bytes
            spans.append((starts[v.lineno - 1] + v.col_offset, starts[v.end_lineno - 1] + v.end_col_offset))
    return spans


def _python_re_compile_spans(text: str) -> list[tuple[int, int]]:
    """Byte spans of bare `compile` call names when `compile` is bound ONLY by `from re|regex import compile`.

    R12: `from re import compile` then `compile(r"...")` is a regular expression, not the builtin. Any other binding
    of the name anywhere in the file (an assignment, a def, another import) keeps every `compile(` refused.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    from_re, other = False, False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) == "compile":
                    if node.module in ("re", "regex") and alias.name == "compile" and not node.level:
                        from_re = True
                    else:
                        other = True
        elif isinstance(node, ast.Import):
            other |= any((a.asname or a.name.split(".")[0]) == "compile" for a in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == "compile":
            other = True
        elif isinstance(node, ast.Name) and node.id == "compile" and isinstance(node.ctx, (ast.Store, ast.Del)):
            other = True
        elif isinstance(node, ast.arg) and node.arg == "compile":
            other = True
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            other |= "compile" in node.names
    if not from_re or other or re.search(r"\r(?!\n)", text):
        return []
    starts = [0]
    for line in text.encode("utf-8").splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return [(starts[n.func.lineno - 1] + n.func.col_offset, starts[n.func.end_lineno - 1] + n.func.end_col_offset)
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "compile"]


def _blank_bytes(text: str, spans: list[tuple[int, int]]) -> str:
    data = bytearray(text.encode("utf-8"))
    for left, right in spans:
        data[left:right] = bytes(10 if x == 10 else 32 for x in data[left:right])
    return data.decode("utf-8", errors="replace")


#: Languages whose comments are `//` and `/* */`, lexed with their string forms. JS-family files also have template
#: literals (with `${}` nesting) and regex literals; the others treat a backtick as a raw string (Go) or ignore it.
_C_LIKE = {".js": "js", ".mjs": "js", ".cjs": "js", ".ts": "js", ".tsx": "js", ".jsx": "js", ".vue": "js",
           ".svelte": "js", ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".cxx": "c", ".hpp": "c", ".hh": "c",
           ".hxx": "c", ".java": "c", ".kt": "c", ".cs": "c", ".swift": "c", ".scala": "c", ".rs": "c",
           ".go": "go", ".php": "c"}
_REGEX_AFTER_WORDS = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case",
                      "do", "else", "yield", "await"}


def _c_like_code_view(text: str, dialect: str) -> str:
    """`text` with `//` and `/* */` comments blanked (newlines kept); `text` itself if it does not lex cleanly."""
    out = list(text)
    i, n = 0, len(text)
    last_sig, last_word = "", ""
    templates: list[int] = []        # brace depth at each open `${`, innermost last (JS)
    depth = 0

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    def skip_quoted(j: int, quote: str) -> int | None:
        j += 1
        while j < n:
            ch = text[j]
            if ch == "\\":
                j += 2
                continue
            if ch == quote:
                return j + 1
            if ch == "\n" and quote != "`":
                return None
            j += 1
        return None

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            end = text.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
            continue
        if ch == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            if end < 0:
                return text
            blank(i, end + 2)
            i = end + 2
            continue
        if ch in "\"'" or (ch == "`" and dialect == "go"):
            j = skip_quoted(i, ch)
            if j is None:
                if dialect == "c" and ch == "'":     # Rust lifetimes / C++14 digit separators: not a string
                    i += 1
                    last_sig = ch
                    continue
                return text
            i, last_sig, last_word = j, ch, ""
            continue
        if ch == "`" and dialect == "js":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "`":
                    break
                if text[j] == "$" and j + 1 < n and text[j + 1] == "{":
                    break
                j += 1
            if j >= n:
                return text
            if text[j] == "`":
                i, last_sig, last_word = j + 1, "`", ""
                continue
            templates.append(depth)     # `${`: back to code until its matching `}`
            depth += 1
            i, last_sig = j + 2, "{"
            continue
        if ch == "}" and templates and depth - 1 == templates[-1]:
            templates.pop()
            depth -= 1
            j = i + 1                    # resume the template text after `}`
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "`":
                    break
                if text[j] == "$" and j + 1 < n and text[j + 1] == "{":
                    break
                j += 1
            if j >= n:
                return text
            if text[j] == "`":
                i, last_sig, last_word = j + 1, "`", ""
            else:
                templates.append(depth)
                depth += 1
                i, last_sig = j + 2, "{"
            continue
        if ch == "/" and dialect == "js" and (last_sig in "" or last_sig in "(,=:[!&|?{};+-*%<>~^"
                                               or last_word in _REGEX_AFTER_WORDS):
            j, in_class = i + 1, False
            while j < n and text[j] != "\n":
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "[":
                    in_class = True
                elif text[j] == "]":
                    in_class = False
                elif text[j] == "/" and not in_class:
                    break
                j += 1
            if j >= n or text[j] != "/":
                return text
            i, last_sig, last_word = j + 1, "/", ""
            continue
        if ch in "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch.isalnum() or ch in "_$":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            last_word, last_sig = text[i:j], text[j - 1]
            i = j
            continue
        if not ch.isspace():
            last_sig, last_word = ch, ""
        i += 1
    return "".join(out)


#: Shell, Ruby and Perl: `#` starts a comment at a line start or after whitespace, outside quotes.
_HASH_COMMENT = {".sh", ".bash", ".zsh", ".rb", ".pl"}


def _hash_code_view(text: str) -> str:
    out_lines = []
    for line in text.split("\n"):
        quote, i, cut = "", 0, None
        while i < len(line):
            ch = line[i]
            if quote:
                if ch == "\\" and quote == '"':
                    i += 2
                    continue
                if ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
            elif ch == "#" and (i == 0 or line[i - 1] in " \t;"):
                cut = i
                break
            i += 1
        if quote and cut is None:
            return text                   # a quote spanning lines (heredoc, multi-line string): scan raw
        out_lines.append(line if cut is None else line[:cut] + " " * (len(line) - cut))
    return "\n".join(out_lines)


def code_view(text: str, suffix: str) -> str:
    """What the literal scans read: `text` with comments (and, for Python, bare string statements) blanked."""
    suffix = suffix.lower()
    if suffix == ".py":
        return _blank_bytes(text, _python_inert_spans(text))
    if suffix in {".html", ".htm", ".xhtml"}:
        return _c_like_code_view(text, "js")      # called on the extracted script sources
    if suffix in _C_LIKE:
        return _c_like_code_view(text, _C_LIKE[suffix])
    if suffix in _HASH_COMMENT:
        return _hash_code_view(text)
    return text


def _scanner_text(text: str, relative: str, baseline: list[str],
                  extra: list[tuple[int, int]] | None = None) -> str:
    """Mask attested old statements, literal missing-dependency unit-test mocks, and the `extra` byte spans
    (comments, bare string statements, `re.compile` call names: `_python_inert_spans`/`_python_re_compile_spans`)."""
    ranges = []
    trusted = set(baseline)
    reserved = {"sys", "builtins", "pytest", "pluggy", "unittest", "conftest"}
    for node, scope in _scoped_syntax(text):
        old_statement = isinstance(node, ast.stmt) and _syntax_signature(node, scope) in trusted
        local_test = "tests" in Path(relative).parts and Path(relative).name.startswith("test_") \
            and scope and scope[-1].startswith("test_")
        literal_mock = False
        if local_test and isinstance(node, ast.Call) and ast.unparse(node.func).endswith("patch.dict") \
                and len(node.args) == 2 and not node.keywords:
            target, changes = node.args
            if isinstance(target, ast.Constant) and target.value == "sys.modules" \
                    and isinstance(changes, ast.Dict) and changes.keys:
                literal_mock = all(isinstance(k, ast.Constant) and isinstance(k.value, str)
                    and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]*", k.value)
                    and not any(x.startswith("_") for x in k.value.split("."))
                    and k.value.split(".")[0] not in reserved
                    and isinstance(v, ast.Constant) and v.value is None
                    for k, v in zip(changes.keys, changes.values))
                if literal_mock:
                    node = target  # Only the benign string, never the whole test.
        if old_statement or literal_mock:
            ranges.append((node.lineno, node.col_offset, node.end_lineno, node.end_col_offset))
    # AST columns count UTF-8 bytes, including in Persian source.
    lines = text.encode("utf-8").splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    data = bytearray(text.encode("utf-8"))
    for first, start, last, end in ranges:
        left, right = offsets[first-1]+start, offsets[last-1]+end
        data[left:right] = bytes(10 if x == 10 else 32 for x in data[left:right])
    for left, right in extra or ():
        data[left:right] = bytes(10 if x == 10 else 32 for x in data[left:right])
    return data.decode("utf-8", errors="replace")


def _unattested_ctypes_reference(text: str, baseline: list[str]) -> bool:
    """Actual module references matter; inert comments and strings do not."""
    syntax = _scoped_syntax(text)
    trusted = set(baseline)
    ranges = [(n.lineno, n.col_offset, n.end_lineno, n.end_col_offset)
              for n, scope in syntax if isinstance(n, ast.stmt)
              and _syntax_signature(n, scope) in trusted]
    for node, _ in syntax:
        if not isinstance(node, ast.Name) or node.id != "ctypes":
            continue
        location = (node.lineno, node.col_offset)
        if not any((a, b) <= location < (c, d) for a, b, c, d in ranges):
            return True
    return False


def introspection_problems(tree: Path, manifest: dict, vendored: list[str] | tuple[str, ...] = (),
                           reachable: set[str] | None = None) -> list[str]:
    """Files the agent wrote or changed that reach into the test process or the hidden tree.

    Returns "<path>: <token>" for every such file. Unchanged fixture files
    (hash equal to the manifest's) are not scanned. Directory names do not
    establish trusted dependencies: newly added or changed executable source
    is scanned even below dependency-like names or declared vendored paths.
    Controller-restored pristine dependencies are symlinked from the
    read-only dependency image; rglob never follows those directory links. Python
    files are scanned for the interpreter primitives as well; every source
    language is scanned for the hidden-tree tokens.
    """
    tree = Path(tree)
    problems = []
    for path in sorted(p for p in tree.rglob("*") if p.suffix.lower() in SOURCE_SUFFIXES):
        rel = path.relative_to(tree).as_posix()
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if rel in manifest and _sha256(path) == manifest[rel].get("sha256"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if path.suffix.lower() in {".html", ".htm", ".xhtml"}:
            text = _html_execution_sources(text)
        # Comments and inert docstrings cannot run (Arena 2026-10-11, R01/R04/R06/R08/R12/R13): scan the code view.
        code = code_view(text, path.suffix)
        if path.suffix.lower() == ".py":
            inert = _python_inert_spans(text)
            scanned = _scanner_text(text, rel, manifest.get(rel, {}).get("benign_syntax", []),
                                    extra=inert + _python_re_compile_spans(text))
        else:
            scanned = code
        python = path.suffix.lower() == ".py" and (reachable is None or rel in reachable)
        hit = _HIDDEN_TREE.search(code) or (_INTROSPECTION.search(scanned) if python else None)
        if hit:
            problems.append(f"{rel}: {hit.group(0)}")
        elif python and _unattested_ctypes_reference(
                text, manifest.get(rel, {}).get("benign_syntax", [])):
            problems.append(f"{rel}: ctypes")
    return problems


def run_command(check: dict, tree: Path, toolchain: dict, manifest: dict | None = None) -> dict:
    runner = check["runner"]
    timeout = float(runner.get("timeout_s", 120))
    if tree.is_dir():
        reaching = scan_problems(tree, manifest or {}, toolchain.get("vendored", []), runner,
                                 HERE / "suites")
        if reaching:
            return {"passed": False, "exit_code": None, "stdout": "", "stderr": "", "seconds": 0.0,
                    "error": "grader: code the hidden tests would import reaches into the test process, so the "
                             "suite was not run over it: " + "; ".join(reaching[:4])}
    # Test-runner processes start in /tests/suites; a `command` runner starts
    # in the tree (its command is ours, and it usually runs the agent's code).
    cwd = (tree / runner.get("cwd", ".")) if runner["type"] == "command" else (HERE / "suites")
    # Neither the working tree nor the home directory may name the check: a
    # reviewer showed a backend branching on the check id read from ARENA_REPO
    # (2026-09-30). Both are opaque per run.
    import secrets as _secrets
    home = Path("/tmp/arena-grade-home") / _secrets.token_hex(8)
    home.mkdir(parents=True, exist_ok=True)
    ids = _grader_ids()
    # The report lives in a fresh private directory and is named nowhere the
    # agent's code can read (not argv, not the environment): the bootstrap
    # receives it on stdin together with a per-run nonce the report must echo.
    import secrets
    import tempfile
    report_dir = Path(tempfile.mkdtemp(prefix="arena-report-"))
    nonce = "arena-" + secrets.token_hex(12)
    if ids is not None:
        try:
            for owned in (home, report_dir, tree):
                _chown_tree(owned, ids)
        except PermissionError as exc:
            # Fail closed, and say why: a verifier without CAP_CHOWN/SETUID/SETGID
            # cannot isolate the hidden tests from the code they import.
            shutil.rmtree(report_dir, ignore_errors=True)
            return {"passed": False, "exit_code": None, "stdout": "", "stderr": "", "seconds": 0.0,
                    "error": f"grader: cannot hand the check's tree to the unprivileged user ({exc}); "
                             "the verifier container needs CAP_CHOWN, CAP_SETUID and CAP_SETGID"}
    select = list(runner.get("select") or [])
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
           "HOME": str(home), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "PYTHONNOUSERSITE": "1",
           "TZ": "UTC", "NODE_OPTIONS": "", "NPM_CONFIG_UPDATE_NOTIFIER": "false",
           "ARENA_REPO": str(tree), "ARENA_TESTS": str(HERE),
           "ARENA_HISTORY": os.environ.get("ARENA_HISTORY", "/arena-history"),
           "ARENA_STEP": os.environ.get("ARENA_STEP", "")}
    env.update(toolchain.get("env", {}))
    env.update(runner.get("env", {}))
    env.pop("PYTHONPATH", None)
    for leak in ("PYTEST_CURRENT_TEST", "PYTEST_ADDOPTS", "ARENA_CHECK", "ARENA_CHECK_ID"):
        env.pop(leak, None)
    started = time.monotonic()
    stdin_payload = None
    try:
        argv, is_shell = command_for(runner, tree, report_dir=report_dir)
        if runner["type"] == "pytest":
            nodes = []
            for item in select:
                file_part, _, test_part = item.partition("::")
                nodes.append(_suite(file_part) + ("::" + test_part if test_part else ""))
            stdin_payload = json.dumps({"nodes": nodes, "args": list(runner.get("args", [])),
                                        "report": str(report_dir / "junit.xml"), "nonce": nonce,
                                        "suites": str(HERE / "suites")}) + "\n"
        elif runner["type"] == "unittest":
            stdin_payload = json.dumps({"nodes": select, "report": str(report_dir / "unittest.json"),
                                        "nonce": nonce, "suites": str(HERE / "suites")}) + "\n"
        # The interpreter is the one the toolchain's PATH names (the verifier
        # venv with the test dependencies), never whichever python3 happened
        # to start this grader.
        if not is_shell and argv and argv[0] == sys.executable:
            argv[0] = shutil.which("python3", path=env["PATH"]) or sys.executable
    except ValueError as exc:
        return {"passed": False, "exit_code": None, "error": f"grader: {exc}",
                "stdout": "", "stderr": "", "seconds": 0.0}
    try:
        proc = subprocess.Popen(argv, shell=is_shell, cwd=str(cwd), env=env,
                                stdin=subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, preexec_fn=_drop_privileges(ids))
    except OSError as exc:
        return {"passed": False, "exit_code": None, "error": f"could not start runner: {exc}",
                "stdout": "", "stderr": "", "seconds": 0.0}
    try:
        out, err = proc.communicate(input=stdin_payload.encode() if stdin_payload is not None else None,
                                    timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = proc.communicate()
        timed_out = True
    seconds = round(time.monotonic() - started, 3)
    stdout = _tail(out.decode("utf-8", "replace"))
    stderr = _tail(err.decode("utf-8", "replace"))
    record = {"exit_code": proc.returncode, "stdout": stdout, "stderr": stderr,
              "seconds": seconds, "timed_out": timed_out}
    if timed_out:
        record.update(passed=False, error=f"timed out after {timeout:.0f}s")
    elif runner["type"] == "pytest" and proc.returncode == 5:
        # pytest 5 = no tests collected: the selection names nothing. That is
        # our defect, and it must never read as the agent's failure.
        record.update(passed=False, error="grader: pytest collected no tests for this selection")
    elif runner["type"] in COUNTED_RUNNERS:
        counts = test_counts(runner["type"], stdout, stderr, report_dir, tree, nonce=nonce, select=select)
        record["tests"] = counts
        if counts is None:
            record.update(passed=False, error="runner produced no trustworthy report of completed tests for this "
                                              "selection; exit status alone is not evidence")
        elif counts["passed"] < 1 or counts["failed"] or counts["skipped"]:
            record["passed"] = False
            if proc.returncode == 0:
                record["error"] = (f"exit status 0 but {counts['passed']} passed, {counts['failed']} failed, "
                                   f"{counts['skipped']} skipped: a test that did not complete is not a pass")
        else:
            record["passed"] = proc.returncode == 0
    else:
        record["passed"] = proc.returncode == 0
    shutil.rmtree(report_dir, ignore_errors=True)
    return record


# ---------------------------------------------------------------------------
# Deliverable contracts
# ---------------------------------------------------------------------------

def _artifact_fd(relative: str, repo: Path) -> int:
    """Open a declared regular file without dereferencing any agent-owned link.

    Open each parent relative to its retained directory descriptor: swapping a
    checked parent for a symlink cannot redirect a privileged JSON read. Global
    aliases in the verifier's trusted repo root are resolved before this walk.
    """
    if not isinstance(relative, str) or not relative or '\\' in relative or '\0' in relative:
        raise ValueError('invalid artifact path')
    path = Path(relative)
    if '..' in path.parts:
        raise ValueError('artifact path contains parent traversal')
    lexical_repo = repo.absolute()
    if not path.is_absolute():
        root, parts = repo.resolve(), path.parts
    elif path.is_relative_to(lexical_repo):
        root, parts = repo.resolve(), path.relative_to(lexical_repo).parts
    else:
        root, parts = Path(path.anchor), path.parts[1:]
    if not parts:
        raise ValueError('artifact path names a directory')
    flags = os.O_RDONLY | os.O_NOFOLLOW
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts[:-1]:
            child = os.open(component, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError('declared artifact must be a regular file')
        return fd
    finally:
        os.close(directory)


def contract_errors(deliverables: list[dict], repo: Path) -> list[str]:
    errors = []
    for item in deliverables or []:
        try:
            fd = _artifact_fd(item['path'], repo)
            with os.fdopen(fd, encoding='utf-8') as handle:
                if item.get('kind', 'file') == 'json':
                    json.load(handle)
        except (OSError, ValueError, UnicodeError) as exc:
            errors.append(f"declared deliverable {item['path']} is missing, nonregular, linked, or invalid: {exc}")
    return errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def order(checks: list[dict]) -> list[dict]:
    """Guardrails first, then objectives in prerequisite order (cycles raise)."""
    by_id = {c["id"]: c for c in checks}
    done, out = set(), []
    for check in checks:
        if check["kind"] == "guardrail":
            out.append(check)
            done.add(check["id"])
    remaining = [c for c in checks if c["kind"] != "guardrail"]
    while remaining:
        progressed = False
        for check in list(remaining):
            needs = [r for r in check.get("requires", []) if r not in done]
            if any(r not in by_id for r in needs):
                raise ValueError(f"{check['id']} requires an undeclared check")
            if not needs:
                out.append(check)
                done.add(check["id"])
                remaining.remove(check)
                progressed = True
        if not progressed:
            raise ValueError("requires cycle among: " + ", ".join(c["id"] for c in remaining))
    return out


def grade(step: str, checks_doc: dict, manifest: dict, repo: Path, work: Path) -> dict:
    toolchain = checks_doc.get("toolchain", {})
    errors: list[str] = []
    restore_tree(repo, toolchain, errors)
    if repo.is_dir():
        links = escaping_symlinks(repo)
        if links:
            errors.append("contract: symlinks escape the working tree: " + ", ".join(links[:5]))
        errors.extend(contract_errors(checks_doc.get("deliverables", []), repo))
    checks = [c for c in checks_doc["checks"] if c.get("step") == step]
    results: dict[str, dict] = {}
    passed_ids: set[str] = set()
    guardrail_failed = False
    for check in order(checks):
        cid = check["id"]
        gated = [r for r in check.get("requires", []) if r not in passed_ids]
        if gated:
            results[cid] = {"kind": check["kind"], "weight": check["weight"], "passed": False,
                            "gated_out": True, "reason": "prerequisite failed: " + ", ".join(gated)}
            continue
        if errors and check["kind"] != "guardrail":
            results[cid] = {"kind": check["kind"], "weight": check["weight"], "passed": False,
                            "gated_out": False, "reason": "contract error"}
            continue
        runner_type = check["runner"]["type"]
        if runner_type in STRUCTURAL:
            if not repo.is_dir():
                ok, why = False, "working tree missing"
            else:
                try:
                    ok, why = structural(check, repo, manifest)
                except (OSError, ValueError) as exc:
                    ok, why = False, f"grader: {exc}"
            record = {"kind": check["kind"], "weight": check["weight"], "passed": ok,
                      "gated_out": False, "reason": why}
        else:
            if not repo.is_dir():
                record = {"kind": check["kind"], "weight": check["weight"], "passed": False,
                          "gated_out": False, "reason": "working tree missing"}
            else:
                import secrets as _secrets
                tree = fresh_copy(repo, work / ("t-" + _secrets.token_hex(8)), toolchain.get("vendored", []))
                excluded_tests = (exclude_added_python_tests(tree, manifest)
                                  if runner_type in ("pytest", "unittest") else [])
                excluded_envs = exclude_added_virtualenvs(tree, manifest)
                excluded_conftests = exclude_added_conftests(tree, manifest)
                run = run_command(check, tree, toolchain, manifest)
                record = {"kind": check["kind"], "weight": check["weight"],
                          "passed": bool(run.get("passed")), "gated_out": False,
                          "reason": run.get("error") or ("exit 0" if run.get("passed") else f"exit {run.get('exit_code')}"),
                          "exit_code": run.get("exit_code"), "seconds": run.get("seconds"),
                          "stdout": run.get("stdout", ""), "stderr": run.get("stderr", ""),
                          "timed_out": run.get("timed_out", False)}
                if excluded_tests:
                    record["excluded_local_test_files"] = excluded_tests
                if excluded_envs:
                    record["excluded_virtualenvs"] = excluded_envs
                if excluded_conftests:
                    record["excluded_conftests"] = excluded_conftests
                if run.get("error"):
                    record["error"] = run["error"]
                shutil.rmtree(tree, ignore_errors=True)
        results[cid] = record
        if record["passed"]:
            passed_ids.add(cid)
        elif check["kind"] == "guardrail":
            guardrail_failed = True
    total = sum(c["weight"] for c in checks if c["kind"] == "objective")
    earned = sum(c["weight"] for c in checks
                 if c["kind"] == "objective" and results.get(c["id"], {}).get("passed"))
    reward = 0.0 if (guardrail_failed or errors or not total) else round(earned / total, 4)
    return {"schema": SCHEMA, "step": step, "reward": reward, "earned": earned, "total": total,
            "guardrail_violated": guardrail_failed, "contract_errors": errors,
            "checks": results}


def main() -> int:
    checks_doc = _load_json(HERE / "checks.json")
    if checks_doc.get("schema") != CHECKS_SCHEMA:
        raise ValueError(f"checks.json must be {CHECKS_SCHEMA}")
    rubric = _load_json(HERE / "rubric.json")
    manifest = _load_json(HERE / "fixture-manifest.json")
    step = os.environ.get("ARENA_STEP", "").strip()
    if not step and len(rubric) == 1:
        step = next(iter(rubric))
    if step not in rubric:
        raise ValueError("ARENA_STEP must name a declared step")
    wanted = set(rubric[step])
    declared = {c["id"] for c in checks_doc["checks"] if c.get("step") == step}
    if declared != wanted:
        raise ValueError(f"rubric.json and checks.json disagree for step {step}")
    repo = Path(os.environ.get("ARENA_REPO", "/app/repo"))
    work = Path(os.environ.get("ARENA_GRADE_WORK", "/tmp/arena-grade"))
    work.mkdir(parents=True, exist_ok=True)
    result = grade(step, checks_doc, manifest, repo, work)
    out = Path(os.environ.get("ARENA_REWARD", "/logs/verifier/reward.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"reward": float(result["reward"])}))
    details = os.environ.get("ARENA_DETAILS")
    if details:
        Path(details).write_text(json.dumps({"reward": {"kind": "programmatic",
            "score": result["reward"], "criteria": [
                {"name": cid, "value": float(bool(rec.get("passed"))),
                 "status": "evaluated", "gated_out": bool(rec.get("gated_out")),
                 "reason": rec.get("reason", "")}
                for cid, rec in result["checks"].items()]}}))
    json.dump(result, sys.stdout, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
