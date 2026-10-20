"""Executed validity controls for a coding bundle, through the shipped grader.

Every claim a bundle makes about its grader is checked by running it:

  nop            the untouched fixture: every objective fails, every guardrail
                 holds, reward 0
  golden         the reference solution: reward 1.0 on every step
  determinism    the reference again, `policy.DETERMINISM_RUNS` times: the
                 per-check flags must be identical (a flaky test is a defect)
  alternative    an independently written second correct fix: 1.0. This is what
                 proves the tests grade behaviour, not the reference's text
  mutant:<id>    each plausible wrong fix: fails exactly the checks its trap
                 declares, passes at least one objective, loses at most
                 policy.MISREADING_MAX_OBJECTIVE_SHARE of the objectives
  coverage       every core/stretch objective is failed by at least one mutant
  empty          the working tree emptied: reward 0 (contract or guardrail)
  destructive    the source deleted, README kept: reward 0
  test_tamper    the reference plus every agent-side tampering the scan looks
                 for (repo conftest/pytest.ini/sitecustomize, a fake reward
                 file, a `tests/` directory, a shadowing package): the reward
                 must equal golden's, i.e. none of it reaches the verifier
  neutral_additions  the reference on every step plus what an honest agent
                 commonly leaves beside a correct fix (a local conftest.py with
                 an unused fixture, one passing test in the repo's own runner,
                 NOTES.md, an empty __pycache__/): every check reads as it does
                 for the reference, so no guardrail fails on files outside its
                 sentence. A tree-guard's published file budget is an advisory
  prefix_only:<s>  (two or more steps) the reference for every step before s,
                 nothing at s: no objective of step s passes. A later turn must
                 need new work (Spec Appendix A: every turn is substantive)
  skip_prior:<s>   (two or more steps) nothing before s, then s's reference
                 patch where it applies to the untouched fixture: step s is not
                 full credit. A later turn builds on the state earlier turns left

Guardrail isolation needs no trial of its own: a mutant must fail exactly the
checks it declares, all inside its trap's checks, so a guardrail it breaks
without its trap declaring it is already a mutant error.

The receipt is `verification/controls.json` (`arena-coding/controls/1`),
bound to the artifact hash. Nothing here decides difficulty.
"""
from __future__ import annotations

import json
import posixpath
import re
import shlex
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import policy
from ..artifacts import atomic_json, identity
from . import control_cache, sandbox
from .build import load_spec
from .checks import load_shipped
from .contracts import contained

SCHEMA = "arena-coding/controls/1"
RECEIPT = "verification/controls.json"

#: Per toolchain: prepend an unconditional successful exit to every source file
#: of the agent's tree (vendored links excluded). Compiled toolchains have no
#: equivalent one-line planting and are recorded as an untested surface.
_PREPEND = ("find /app/repo -type f {names} -not -path '*/node_modules/*' -not -path '*/.venv/*' "
            "-not -path '*/venv/*' -not -path '*/vendor/*' | while IFS= read -r f; do "
            "{{ printf %s {line}; cat \"$f\"; }} > \"$f.arena-exit\" && mv \"$f.arena-exit\" \"$f\"; done")
EARLY_EXIT_SHELL = {
    # The python planting also forges every report file it can reach before
    # exiting -- the attack the reviewer described (2026-09-27): a fake JUnit
    # result written by imported code. The grader must not count it.
    "python": _PREPEND.format(names="-name '*.py'", line=json.dumps(
        "import glob as _g, os as _o\n"
        "for _p in _g.glob('/tmp/**/*.xml', recursive=True) + _g.glob('/tmp/**/*.json', recursive=True):\n"
        "    try:\n"
        "        open(_p, 'w').write('<testsuite name=\"forged\" tests=\"1\" failures=\"0\" errors=\"0\" skipped=\"0\">"
        "<testcase classname=\"forged\" name=\"forged\"/></testsuite>')\n"
        "    except OSError:\n"
        "        pass\n"
        "_o._exit(0)\n")),
    "node": _PREPEND.format(names="\\( -name '*.js' -o -name '*.mjs' -o -name '*.cjs' -o -name '*.ts' "
                                  "-o -name '*.tsx' -o -name '*.jsx' \\)", line=json.dumps("process.exit(0);\n")),
    "browser": _PREPEND.format(names="\\( -name '*.js' -o -name '*.mjs' -o -name '*.cjs' -o -name '*.ts' "
                                     "-o -name '*.tsx' -o -name '*.jsx' \\)", line=json.dumps("process.exit(0);\n")),
    "php": _PREPEND.format(names="-name '*.php'", line=json.dumps("<?php exit(0); ?>\n")),
}


def _go_initializer_shell(body: str) -> str:
    """Plant valid Go initializers without invalidating import declarations."""
    import shlex
    program = (
        "import os,re\nfrom pathlib import Path\n"
        "for root,dirs,files in os.walk('/app/repo'):\n"
        "    dirs[:]=[d for d in dirs if d not in ('vendor','node_modules','.git','.venv')]\n"
        "    for name in sorted(files):\n"
        "        if not name.endswith('.go') or name.endswith('_test.go'): continue\n"
        "        source=(Path(root)/name).read_text(errors='replace')\n"
        "        found=re.search(r'^package\\s+([\\w]+)',source,re.M)\n"
        "        if found:\n"
        "            (Path(root)/'arena_control_initializer.go').write_text('package '+found.group(1)+'\\nimport(\"os\";\"fmt\")\\nfunc init(){'+"
        + repr(body) + "+'}\\n')\n"
        "            break\n"
    )
    return "python3 -c " + shlex.quote(program)


EARLY_EXIT_SHELL["go"] = _go_initializer_shell('_=fmt.Sprint(0);os.Exit(0)')

#: Reviewer finding S1 (2026-09-30), reproduced: imported agent code read the
#: bootstrap's configuration from `__main__.cfg` -- report path, nonce and the
#: selected nodes -- wrote a report the grader trusted, and exited. This
#: planting hunts the run's identity everywhere the old runtime left it: the
#: `__main__` namespace, the environment, argv, stdin and the locals of every
#: live frame; then it walks the garbage collector for the reporter itself
#: (the route the second review named) and for any closure holding a nonce;
#: and it rewrites the test framework itself (the third review's route:
#: pytest's TestReport and unittest's TestCase.run record every test as
#: passed), which the runtime's framework-integrity snapshot must catch; and it
#: reaches the hidden suite by ordinary import to empty its case lists and
#: replace its assertion helpers (the fourth review's route), which the
#: runtime's detachment of the suite after collection must defeat;
#: whatever it finds, it forges a report for and exits 0. On the untouched
#: fixture every objective must still fail: the runtime refuses to run a
#: suite over a changed file that reaches into the interpreter (grade.py
#: `introspection_problems`), so the planted file never gets to hunt.
REPORT_FORGE_SHELL = {
    "python": _PREPEND.format(names="-name '*.py'", line=json.dumps(
        "import os as _o, sys as _s, json as _j\n"
        "def _arena_hunt():\n"
        "    found = {}\n"
        "    try:\n"
        "        import __main__ as _m\n"
        "        for _k, _v in list(vars(_m).items()):\n"
        "            if isinstance(_v, dict) and 'nonce' in _v:\n"
        "                found.update({x: _v.get(x) for x in ('nonce', 'report', 'nodes')})\n"
        "            elif isinstance(_v, str) and _v.startswith('arena-'):\n"
        "                found.setdefault('nonce', _v)\n"
        "    except Exception:\n"
        "        pass\n"
        "    for _v in list(_o.environ.values()) + list(_s.argv):\n"
        "        if isinstance(_v, str) and _v.startswith('arena-') and len(_v) > 20:\n"
        "            found.setdefault('nonce', _v)\n"
        "        if isinstance(_v, str) and _v.endswith(('junit.xml', 'unittest.json')):\n"
        "            found.setdefault('report', _v)\n"
        "    try:\n"
        "        _line = _s.stdin.readline()\n"
        "        _d = _j.loads(_line) if _line.strip().startswith('{') else {}\n"
        "        if isinstance(_d, dict) and 'nonce' in _d:\n"
        "            found.update({x: _d.get(x) for x in ('nonce', 'report', 'nodes')})\n"
        "    except Exception:\n"
        "        pass\n"
        "    try:\n"
        "        _f = _s._getframe()\n"
        "        while _f is not None:\n"
        "            for _k, _v in list(_f.f_locals.items()):\n"
        "                if _k in ('cfg',) and isinstance(_v, dict) and 'nonce' in _v:\n"
        "                    found.update({x: _v.get(x) for x in ('nonce', 'report', 'nodes')})\n"
        "                elif _k == 'nonce' and isinstance(_v, str):\n"
        "                    found.setdefault('nonce', _v)\n"
        "                elif _k in ('report', 'report_path') and isinstance(_v, str):\n"
        "                    found.setdefault('report', _v)\n"
        "            _f = _f.f_back\n"
        "    except Exception:\n"
        "        pass\n"
        "    return found\n"
        "def _arena_reach():\n"
        "    # the reviewer's route (2026-09-30): find the reporter itself and make it write\n"
        "    try:\n"
        "        import gc as _gc\n"
        "        _rep = _cfgobj = None\n"
        "        for _obj in _gc.get_objects():\n"
        "            if hasattr(_obj, 'pytest_sessionfinish') and hasattr(_obj, 'pytest_runtest_logreport'):\n"
        "                _rep = _obj\n"
        "            elif type(_obj).__name__ == 'Config' and hasattr(_obj, 'invocation_params'):\n"
        "                _cfgobj = _obj\n"
        "        if _rep is not None:\n"
        "            _fn = _rep.pytest_sessionfinish\n"
        "            _cells = dict(zip(_fn.__code__.co_freevars, [_c.cell_contents for _c in (_fn.__closure__ or ())]))\n"
        "            _outs = _cells.get('outcomes')\n"
        "            _args = list(getattr(getattr(_cfgobj, 'invocation_params', None), 'args', None) or [])\n"
        "            _nodes = [_a for _a in _args if '::' in _a or _a.endswith('.py')] or ['suites/forged.py::test_forged']\n"
        "            if isinstance(_outs, dict):\n"
        "                for _n in _nodes:\n"
        "                    _outs[_n] = 'passed'\n"
        "                _fn(None, 0)\n"
        "                _o._exit(0)\n"
        "        for _obj in _gc.get_objects():\n"
        "            _code = getattr(_obj, '__code__', None)\n"
        "            if _code is not None and 'nonce' in getattr(_code, 'co_freevars', ()) and getattr(_obj, '__closure__', None):\n"
        "                _cells = dict(zip(_code.co_freevars, [_c.cell_contents for _c in _obj.__closure__]))\n"
        "                _p = _cells.get('report') or _cells.get('report_path')\n"
        "                if _p and _cells.get('nonce'):\n"
        "                    return {'nonce': _cells['nonce'], 'report': _p, 'nodes': []}\n"
        "    except Exception:\n"
        "        pass\n"
        "    return {}\n"
        "def _arena_rewire():\n"
        "    # the third route (2026-09-30): rewrite the framework so honest reporting records passes\n"
        "    try:\n"
        "        _mod = _s.modules.get('_pytest.reports')\n"
        "        if _mod is None:\n"
        "            import _pytest.reports as _mod\n"
        "        _orig = _mod.TestReport.__init__\n"
        "        def _init(self, *a, **k):\n"
        "            _orig(self, *a, **k)\n"
        "            self.outcome = 'passed'\n"
        "            self.longrepr = None\n"
        "        _mod.TestReport.__init__ = _init\n"
        "    except Exception:\n"
        "        pass\n"
        "    try:\n"
        "        import unittest as _u\n"
        "        def _run(self, result=None):\n"
        "            if result is not None:\n"
        "                result.startTest(self); result.addSuccess(self); result.stopTest(self)\n"
        "            return result\n"
        "        _u.TestCase.run = _run\n"
        "    except Exception:\n"
        "        pass\n"
        "def _arena_gut_suite():\n"
        "    # the fourth route (2026-09-30): reach the hidden suite by ordinary import and empty it\n"
        "    try:\n"
        "        _names = [_n[:-3] for _n in _o.listdir('/tests/suites') if _n.endswith('.py')]\n"
        "    except Exception:\n"
        "        _names = []\n"
        "    for _n in _names + [_k for _k, _v in list(_s.modules.items()) if str(getattr(_v, '__file__', '')).startswith('/tests/')]:\n"
        "        try:\n"
        "            _mod = _s.modules.get(_n) or __import__(_n)\n"
        "            for _k, _v in list(vars(_mod).items()):\n"
        "                if isinstance(_v, list):\n"
        "                    _v.clear()\n"
        "                elif callable(_v) and (_k.startswith('assert') or _k.startswith('check') or _k.startswith('_assert')):\n"
        "                    setattr(_mod, _k, lambda *a, **k: True)\n"
        "        except Exception:\n"
        "            pass\n"
        "_arena_gut_suite()\n"
        "_arena_rewire()\n"
        "_h = _arena_hunt() or _arena_reach()\n"
        "if _h.get('nonce') and _h.get('report'):\n"
        "    _nodes = list(_h.get('nodes') or []) or ['suites/forged.py::test_forged']\n"
        "    _cases = ''.join('<testcase classname=\"%s\" name=\"%s\"/>' % (_o.path.splitext(_o.path.basename(_n.partition('::')[0]))[0], (_n.partition('::')[2].split('::')[-1] or 'test_forged')) for _n in _nodes)\n"
        "    try:\n"
        "        if str(_h['report']).endswith('.json'):\n"
        "            _j.dump({'nonce': _h['nonce'], 'ran': _nodes, 'testsRun': len(_nodes), 'failures': 0, 'errors': 0, 'skipped': 0, 'bad': []}, open(_h['report'], 'w'))\n"
        "        else:\n"
        "            open(_h['report'], 'w').write('<?xml version=\"1.0\"?><testsuites><testsuite name=\"%s\" tests=\"%d\">%s</testsuite></testsuites>' % (_h['nonce'], len(_nodes), _cases))\n"
        "    except OSError:\n"
        "        pass\n"
        "    _o._exit(0)\n")),
}

REPORT_FORGE_SHELL["go"] = _go_initializer_shell(
    'fmt.Println(`{"Action":"pass","Test":"TestArenaForged"}`);'
    'fmt.Println("=== RUN   TestArenaForged\\n--- PASS: TestArenaForged (0.00s)\\nPASS");os.Exit(0)')

TAMPER_FILES = {
    "conftest.py": "import pytest\n\ndef pytest_collection_modifyitems(items):\n    for item in items:\n        item.add_marker(pytest.mark.skip(reason='tamper'))\n",
    "pytest.ini": "[pytest]\naddopts = -p no:python\n",
    "sitecustomize.py": "import sys\nsys.exit(0)\n",
    "usercustomize.py": "import sys\nsys.exit(0)\n",
    "reward.json": '{"reward": 1.0}\n',
    "tests/test_report.py": "def test_ok():\n    assert True\n",
    "pytest/__init__.py": "raise SystemExit(0)\n",
    "json/__init__.py": "raise SystemExit(0)\n",
    ".pth": "",
}

#: User, 2026-10-21 (external audit, class 6): what an honest agent commonly leaves beside a correct
#: fix. grade.py removes an added conftest.py and added test_*.py from every grading copy, so none of it
#: can reach a hidden test, yet three shipped tasks (a_f1_pptx-generator-script, a_f2_3d-scene-pipeline,
#: a_f1_vanilla-js-pwa) list `conftest` in g_no_shadow's modules and scored their own reference 0 once a
#: tests/conftest.py was added; no control added a harmless file. Each file is written only where the
#: tree has none, so the control never edits a fixture or reference file.
NEUTRAL_CONFTEST = ("import pytest\n\n\n@pytest.fixture\ndef sample_notes():\n"
                    "    return {\"kept\": \"while fixing\"}\n")
NEUTRAL_NOTES = ("NOTES.md", "# Notes\n\nWhat I checked while fixing this. Nothing here is read by the code.\n")
#: The extra passing test, in the repository's own runner where one is recognisable: pytest for a
#: Python toolchain; for node and browser the runner a fixture package.json names, else node:test.
#: Other toolchains get none (recorded in the receipt).
NEUTRAL_TESTS = {
    "pytest": ("test_zz_notes.py", "def test_notes_are_kept():\n    assert \"notes\" in \"working notes\"\n"),
    "vitest": ("zz_notes.test.mjs", "import { test, expect } from \"vitest\";\n\n"
                                    "test(\"notes are kept\", () => {\n  expect(\"working notes\").toContain(\"notes\");\n});\n"),
    "jest": ("zz_notes.test.js", "test(\"notes are kept\", () => {\n  expect(\"working notes\").toContain(\"notes\");\n});\n"),
    "mocha": ("zz_notes.test.cjs", "const assert = require(\"node:assert\");\n\n"
                                   "it(\"notes are kept\", () => {\n  assert.ok(\"working notes\".includes(\"notes\"));\n});\n"),
    "node:test": ("zz_notes.test.mjs", "import test from \"node:test\";\nimport assert from \"node:assert/strict\";\n\n"
                                       "test(\"notes are kept\", () => {\n  assert.ok(\"working notes\".includes(\"notes\"));\n});\n"),
}


def _repo_test_runner(repo: Path, chain: str | None) -> str | None:
    if chain == "python":
        return "pytest"
    if chain not in ("node", "browser"):
        return None
    # A runner the fixture declares as a package or invokes from a script; `eslint-plugin-jest` is neither.
    names, scripts = set(), []
    for path in sorted(repo.rglob("package.json")):
        if "node_modules" in path.relative_to(repo).parts:
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            continue
        if not isinstance(manifest, dict):
            continue
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            if isinstance(manifest.get(key), dict):
                names.update(manifest[key])
        if isinstance(manifest.get("scripts"), dict):
            scripts += [str(v) for v in manifest["scripts"].values()]
    return next((name for name in ("vitest", "jest", "mocha")
                 if name in names or any(re.search(rf"(^|[\s;&|]){name}\b", s) for s in scripts)), "node:test")


def neutral_additions(bundle: Path, toolchain: dict) -> dict:
    """What the neutral_additions control plants beside the reference: {files, dirs, test_runner}.

    The test directory is the fixture's own (`tests/`, else `test/`), else the toolchain's convention.
    Derived from the bundle alone, so cached evidence re-evaluates to the same plan.
    """
    repo = Path(bundle) / "environment/repo"
    chain = toolchain.get("name")
    tops = {p.name for p in repo.iterdir() if p.is_dir()} if repo.is_dir() else set()
    testdir = next((d for d in ("tests", "test") if d in tops), "test" if chain in ("node", "browser") else "tests")
    conftest = f"{testdir}/conftest.py"
    if (repo / conftest).exists():
        conftest = f"{testdir}/notes/conftest.py"
    files = {conftest: NEUTRAL_CONFTEST, NEUTRAL_NOTES[0]: NEUTRAL_NOTES[1]}
    runner = _repo_test_runner(repo, chain)
    if runner is not None:
        name, text = NEUTRAL_TESTS[runner]
        files[f"{testdir}/{name}"] = text
    return {"files": files, "dirs": ["__pycache__", f"{testdir}/__pycache__"], "test_runner": runner}


def neutral_shell(planted: dict) -> str:
    """The planting, run from the working tree's root; a path the tree already holds is left alone."""
    writes = [f"if [ ! -e {shlex.quote(rel)} ]; then mkdir -p {shlex.quote(posixpath.dirname(rel) or '.')} && "
              f"printf %s {shlex.quote(text)} > {shlex.quote(rel)}; fi" for rel, text in planted["files"].items()]
    return "{ " + "; ".join(writes + ["mkdir -p " + " ".join(shlex.quote(d) for d in planted["dirs"])]) + "; }"


def neutral_variants(bundle: Path, golden: dict[str, sandbox.Variant], steps: list[str],
                     toolchain: dict) -> dict[str, sandbox.Variant]:
    """The reference for every step, each followed by the neutral additions (idempotent across steps)."""
    shell = "cd /app/repo && " + neutral_shell(neutral_additions(bundle, toolchain))
    return {step: sandbox.Variant("shell", label=f"neutral_additions:{step}",
                                  shell=(golden[step].shell + " && " + shell if golden[step].kind == "shell"
                                         else _patch_then(bundle, step, shell)))
            for step in steps}


def neutral_moves(doc: dict, golden_flags: dict, neutral_flags: dict) -> tuple[dict, dict]:
    """Checks the reference passes and the neutral additions fail, per step: (misfires, file_budgets).

    A check whose runner counts new files by design (policy.NEUTRAL_ADDITIONS_EXEMPT_RUNNERS) is a
    file budget, not a misfire; every other moved check is.
    """
    runners = {c["id"]: (c.get("runner") or {}).get("type") for c in doc["checks"]}
    misfires: dict[str, list[str]] = {}
    budgets: dict[str, list[str]] = {}
    for step, reference in golden_flags.items():
        got = neutral_flags.get(step, {})
        for cid in sorted(cid for cid, ok in reference.items() if ok and not got.get(cid)):
            exempt = runners.get(cid) in policy.NEUTRAL_ADDITIONS_EXEMPT_RUNNERS
            (budgets if exempt else misfires).setdefault(step, []).append(cid)
    return misfires, budgets


def _module_leaf(name: str) -> str:
    leaf = str(name).strip().rstrip("/").rsplit("/", 1)[-1]
    return leaf[:-3] if leaf.endswith(".py") else leaf


def shadow_conftest_problems(checks) -> list[str]:
    """Shadow-guards that name a module grade.py exempts as an agent-added file (`conftest`).

    grade.py skips an added conftest.py in a shadow-guard -- it is removed from every grading copy and
    the hidden suites run with --rootdir /tests/suites, so it hooks nothing -- unless the guard lists
    `conftest` (or a path ending in it) among its modules. Read by the grader.data gate (refused) and
    by check_draft (advisory). User, 2026-10-21, after the external audit.
    """
    out = []
    for check in checks or []:
        runner = check.get("runner") if isinstance(check, dict) else None
        if not isinstance(runner, dict) or runner.get("type") != "shadow-guard":
            continue
        listed = [m for m in runner.get("modules") or []
                  if isinstance(m, str) and _module_leaf(m) in policy.SHADOW_GUARD_REFUSED_MODULES]
        if listed:
            out.append(f"check {check.get('id')!r}: the shadow-guard lists {listed} among its modules, which turns "
                       "off grade.py's exemption for an agent-added conftest.py (removed from every grading copy; the "
                       "hidden suites run with --rootdir /tests/suites, so it hooks nothing). A correct fix that adds "
                       "tests/conftest.py for its own tests then scores 0, as it did on three shipped tasks "
                       "(a_f1_pptx-generator-script, a_f2_3d-scene-pipeline, a_f1_vanilla-js-pwa). Drop it from "
                       "modules and name only modules the hidden tests import")
    return out


def _flags(record: dict) -> dict[str, dict[str, bool]]:
    return {step: {cid: bool(r.get("passed")) for cid, r in ((s.get("grade") or {}).get("checks") or {}).items()}
            for step, s in record["steps"].items()}


def _objectives(doc: dict) -> dict[str, dict]:
    return {c["id"]: c for c in doc["checks"] if c["kind"] == "objective"}


def _guardrails(doc: dict) -> dict[str, dict]:
    return {c["id"]: c for c in doc["checks"] if c["kind"] == "guardrail"}


def _patch_then(bundle: Path, step: str, shell: str) -> str:
    """The reference patch for `step` applied in the agent container, then `shell`."""
    patch = (Path(bundle) / "steps" / step / "solution/fix.patch").read_text(encoding="utf-8")
    return ("cd /app/repo && cat > /tmp/arena-ref.patch <<'ARENA_PATCH_EOF'\n" + patch.rstrip("\n") +
            "\nARENA_PATCH_EOF\n(git apply --whitespace=nowarn /tmp/arena-ref.patch || patch -p1 --forward < /tmp/arena-ref.patch) && "
            + shell)


def _patch_variant(bundle: Path, rel: str, label: str) -> sandbox.Variant:
    return sandbox.Variant("patch", patch=(Path(bundle) / rel).read_text(encoding="utf-8"), label=label)


#: Markers the skip_prior variant prints, read back by `_evaluate`.
SKIP_PRIOR_APPLIED, SKIP_PRIOR_NOT_APPLIED = "ARENA_SKIP_PRIOR_APPLIED", "ARENA_SKIP_PRIOR_NOT_APPLIED"


def _skip_prior_variant(bundle: Path, step: str) -> sandbox.Variant:
    """Step `step`'s reference patch on the untouched fixture, without the
    earlier steps. A patch that does not apply leaves the tree untouched (its
    own proof that the turn depends on earlier state); it never aborts the trial."""
    patch = (Path(bundle) / "steps" / step / "solution/fix.patch").read_text(encoding="utf-8")
    shell = ("cd /app/repo && cat > /tmp/arena-skip.patch <<'ARENA_PATCH_EOF'\n" + patch.rstrip("\n")
             + "\nARENA_PATCH_EOF\n"
             "if git apply --check --whitespace=nowarn /tmp/arena-skip.patch >/dev/null 2>&1; then "
             f"git apply --whitespace=nowarn /tmp/arena-skip.patch && echo {SKIP_PRIOR_APPLIED}; "
             "elif patch -p1 --forward --dry-run < /tmp/arena-skip.patch >/dev/null 2>&1; then "
             f"patch -p1 --forward < /tmp/arena-skip.patch >/dev/null && echo {SKIP_PRIOR_APPLIED}; "
             f"else echo {SKIP_PRIOR_NOT_APPLIED}; fi")
    return sandbox.Variant("shell", shell=shell, label=f"skip_prior:{step}")


def _golden_variants(bundle: Path, steps: list[str]) -> dict[str, sandbox.Variant]:
    return {step: sandbox.solve_variant(bundle, step) for step in steps}


def _trial_plan(bundle: Path, spec: dict, doc: dict, steps: list[str], toolchain: dict,
                runs: int) -> dict[str, dict[str, sandbox.Variant]]:
    """Independent trials; checkpoint order inside each trial stays sequential."""
    golden = _golden_variants(bundle, steps)
    plan = {"nop": {}}
    plan.update({f"golden-{i + 1}": golden for i in range(max(1, runs))})
    if all((bundle / f"authoring/alt/{step}.patch").is_file() for step in steps):
        plan["alternative"] = {
            step: _patch_variant(bundle, f"authoring/alt/{step}.patch", f"alt:{step}")
            for step in steps}
    traps = {t["id"]: t for t in (spec.get("traps") or [])}
    step_of = {c["id"]: c.get("step") for c in doc["checks"]}
    for mutant in spec.get("mutants") or []:
        mid = mutant["id"]
        if not (bundle / f"authoring/mutants/{mid}.patch").is_file():
            continue
        driven = list((traps.get(mutant.get("trap")) or {}).get("checks") or [])
        step = mutant.get("step") or (step_of.get(driven[0]) if driven else None) or steps[0]
        variants = {s: golden[s] for s in steps[:steps.index(step)]}
        variants[step] = _patch_variant(bundle, f"authoring/mutants/{mid}.patch", f"mutant:{mid}")
        plan[f"mutant:{mid}"] = variants
    # Spec Appendix A, executed per later step: the turn needs new work, and it
    # needs the state the earlier turns created. Single-step bundles plan none.
    for k, step in enumerate(steps[1:], 1):
        plan[f"prefix_only:{step}"] = {s: golden[s] for s in steps[:k]}
        plan[f"skip_prior:{step}"] = {step: _skip_prior_variant(bundle, step)}
    first = steps[0]
    plan["empty"] = {first: sandbox.Variant("empty", label="empty")}
    plan["destructive"] = {first: sandbox.Variant(
        "shell", shell="find /app/repo -type f ! -name 'README*' ! -name '*.md' -delete", label="destructive")}
    tamper_shell = ("cd /app/repo && " + " && ".join(
        f"mkdir -p \"$(dirname '{rel}')\" && printf %s {json.dumps(text)} > '{rel}'"
        for rel, text in TAMPER_FILES.items() if rel != ".pth")
        + " && printf '' > tamper.pth")
    plan["test_tamper"] = {first: sandbox.Variant("shell", shell=tamper_shell, label="test_tamper")}
    exit_shell = EARLY_EXIT_SHELL.get(toolchain.get("name"))
    if exit_shell is not None:
        plan["early_exit"] = {first: sandbox.Variant("shell", shell=exit_shell, label="early_exit")}
    if toolchain.get("name") == "python":
        benign_shell = ("cd /app/repo && mkdir -p tests && printf %s " + json.dumps(
            "import pytest\n\n\ndef test_arena_benign_regression():\n    assert 1 + 1 == 2\n")
            + " > tests/test_arena_benign_regression.py")
        benign = dict(golden)
        shell = (golden[first].shell + " && " + benign_shell
                 if golden[first].kind == "shell" else _patch_then(bundle, first, benign_shell))
        benign[first] = sandbox.Variant("shell", shell=shell, label="benign_test")
        plan["benign_test"] = benign
    # User, 2026-10-21 (external audit, class 6): one trial, every step graded once.
    plan["neutral_additions"] = neutral_variants(bundle, golden, steps, toolchain)
    forge_shell = REPORT_FORGE_SHELL.get(toolchain.get("name"))
    if forge_shell is not None:
        plan["report_forge"] = {first: sandbox.Variant("shell", shell=forge_shell, label="report_forge")}
    return plan


class _ControlPreflightFailure(sandbox.SandboxError):
    """Actual basic-control failures; the remaining matrix has not run."""

    def __init__(self, records: dict, errors: list[str]):
        self.records, self.errors = records, errors
        super().__init__("control preflight failed: " + "; ".join(errors))


def _preflight_errors(records: dict, plan: dict) -> list[str]:
    errors = []
    for label, record in records.items():
        expected = 0.0 if label == "nop" else 1.0
        if record.get("valid") is not True or record.get("reward") != expected:
            errors.append(f"{label}: expected valid reward {expected}, got "
                          f"{record.get('reward')} (valid={record.get('valid')})"
                          + (f": {record['variant_error']} -- a later step's patch applies on top of the tree the "
                             "previous steps of the SAME chain leave (alternatives on alternatives, mutants on the "
                             "earlier references)" if record.get("variant_error") else ""))
        if label != "nop":
            steps = record.get("steps", {})
            if set(steps) != set(plan[label]):
                errors.append(f"{label}: incomplete reference checkpoints")
            for step, result in steps.items():
                if (result.get("reward") != 1.0 or result.get("timed_out")
                        or result.get("error") or (result.get("grade") or {}).get("contract_errors")):
                    errors.append(f"{label}/{step}: reference checkpoint failed")
    return errors


def _execute_trials(bundle: Path, plan: dict, work: Path, agent_tag: str, verifier_tag: str,
                    workers: int, *, keep_capture: bool = False) -> dict:
    def execute(label, variants):
        try:
            return sandbox.verify(bundle, variants, work=work / label.replace(":", "-"),
                                  agent_tag=agent_tag, verifier_tag=verifier_tag, keep_capture=keep_capture)
        except sandbox.VariantApplyError as exc:
            # A design defect (an alternative or mutant patch written against the wrong tree): an
            # invalid trial the designer is told about, never a terminal build error.
            return {"schema": "arena-coding/sandbox-trial/1", "bundle": bundle.name, "steps": {},
                    "valid": False, "reward": None, "variant_error": str(exc)[:1500]}

    def phase(labels):
        if workers == 1:
            return {label: execute(label, plan[label]) for label in labels}
        # Every trial owns its containers and logs. Preserve policy order in
        # the receipt regardless of worker completion order.
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="arena-control") as executor:
            futures = {label: executor.submit(execute, label, plan[label]) for label in labels}
            try:
                return {label: future.result() for label, future in futures.items()}
            except BaseException:
                for future in futures.values():
                    future.cancel()
                raise

    basic = [label for label in ("nop", "golden-1", "alternative") if label in plan]
    records = phase(basic)
    errors = _preflight_errors(records, plan)
    if errors:
        raise _ControlPreflightFailure(records, errors)
    records.update(phase([label for label in plan if label not in records]))
    return {label: records[label] for label in plan}


def _evaluate(bundle: Path, spec: dict, doc: dict, steps: list[str], toolchain: dict,
              runs: int, records: dict) -> tuple[dict, list, list]:
    """Apply the same validity rules to retained actual trials or fresh trials."""
    objectives, guardrails = _objectives(doc), _guardrails(doc)
    results: dict = {}
    errors: list[str] = []
    def trial(label: str) -> dict:
        record = records[label]
        entry = {"reward": record["reward"], "valid": record["valid"], "flags": _flags(record),
                 "errors": {s: e.get("error") for s, e in record["steps"].items() if e.get("error")},
                 "contract_errors": {s: (e.get("grade") or {}).get("contract_errors")
                                     for s, e in record["steps"].items()}}
        if not record["valid"]:
            errors.append(f"{label}: the verifier produced no valid reward "
                          + "; ".join(f"{s}: {e}" for s, e in entry["errors"].items())
                          + (f" {record['variant_error']}" if record.get("variant_error") else ""))
        return entry

    # -- nop -----------------------------------------------------------------
    nop = trial("nop")
    results["nop"] = nop
    if nop["valid"]:
        if nop["reward"] != 0.0:
            errors.append(f"nop: the untouched fixture scores {nop['reward']}, not 0")
        for step, flags in nop["flags"].items():
            passing = [cid for cid, ok in flags.items() if ok and cid in objectives]
            if passing:
                errors.append(f"nop/{step}: objectives pass on the untouched fixture: {', '.join(passing)}")
            broken = [cid for cid, ok in flags.items() if not ok and cid in guardrails]
            if broken:
                errors.append(f"nop/{step}: guardrails fail on the untouched fixture: {', '.join(broken)}")
            if any(nop["contract_errors"].get(step) or []):
                errors.append(f"nop/{step}: contract errors on the untouched fixture: {nop['contract_errors'][step]}")

    # -- golden and determinism ---------------------------------------------
    golden_runs = []
    for i in range(max(1, runs)):
        golden_runs.append(trial(f"golden-{i + 1}"))
    golden = golden_runs[0]
    results["golden"] = golden
    if golden["valid"] and golden["reward"] != 1.0:
        failing = {s: [cid for cid, ok in f.items() if not ok] for s, f in golden["flags"].items()}
        errors.append(f"golden: the reference scores {golden['reward']}, not 1.0; failing: {failing}")
    identical = all(r["flags"] == golden["flags"] and r["reward"] == golden["reward"] for r in golden_runs)
    results["determinism"] = {"runs": len(golden_runs), "identical": identical,
                              "rewards": [r["reward"] for r in golden_runs]}
    if not identical:
        errors.append("determinism: the reference did not grade identically on every run (a flaky check)")

    # -- alternative ---------------------------------------------------------
    if "alternative" in records:
        alt = trial("alternative")
        results["alternative"] = alt
        if alt["valid"] and alt["reward"] != 1.0:
            failing = {s: [cid for cid, ok in f.items() if not ok] for s, f in alt["flags"].items()}
            errors.append(f"alternative: an independently written correct fix scores {alt['reward']}; "
                          f"the tests grade the reference's text, not behaviour: {failing}")
        for step in steps:
            ref = (bundle / "steps" / step / "solution/fix.patch").read_text(encoding="utf-8")
            if (bundle / "authoring/alt" / f"{step}.patch").read_text(encoding="utf-8").strip() == ref.strip():
                errors.append(f"alternative/{step}: identical to the reference patch")
    else:
        errors.append("alternative: every step needs an independently written second correct fix "
                      "(authoring/alt/<step>.patch)")

    # -- mutants -------------------------------------------------------------
    traps = {t["id"]: t for t in (spec.get("traps") or [])}
    mutants = spec.get("mutants") or []
    results["mutants"] = {}
    advisories: list[str] = []
    failed_by_some: set[str] = set()
    share_cap = policy.MISREADING_MAX_OBJECTIVE_SHARE
    step_of = {c["id"]: c.get("step") for c in doc["checks"]}
    for mutant in mutants:
        mid = mutant["id"]
        path = bundle / "authoring/mutants" / f"{mid}.patch"
        if not path.is_file():
            errors.append(f"mutant {mid}: authoring/mutants/{mid}.patch is missing")
            continue
        trap = traps.get(mutant.get("trap"))
        driven_ids = list((trap or {}).get("checks") or [])
        step = (mutant.get("step") or (step_of.get(driven_ids[0]) if driven_ids else None) or steps[0])
        entry = trial(f"mutant:{mid}")
        flags = entry["flags"].get(step, {})
        failed = sorted(cid for cid, ok in flags.items() if not ok)
        declared = sorted(mutant.get("fails") or [])
        driven = sorted((trap or {}).get("checks") or [])
        entry.update(step=step, failed=failed, declared=declared, trap=mutant.get("trap"))
        results["mutants"][mid] = entry
        failed_by_some.update(cid for cid in failed if cid in objectives)
        if not entry["valid"]:
            continue
        if failed != declared:
            errors.append(f"mutant {mid}: fails {failed}, declared {declared}")
        outside = [cid for cid in failed if driven and cid not in driven]
        if outside:
            errors.append(f"mutant {mid}: fails checks its trap does not drive: {outside}")
        if not any(cid in driven for cid in failed):
            errors.append(f"mutant {mid}: fails none of the checks its trap drives; the trap is dormant")
        step_objectives = [cid for cid in objectives if objectives[cid].get("step") == step]
        lost = [cid for cid in failed if cid in objectives]
        passed = [cid for cid in step_objectives if flags.get(cid)]
        if not passed:
            errors.append(f"mutant {mid}: passes no objective; that is a wrong answer, not a misreading")
        share = (len(lost) / len(step_objectives)) if step_objectives else 0.0
        entry["objective_share_lost"] = round(share, 3)
        if share > share_cap:
            message = (f"mutant {mid}: loses {len(lost)}/{len(step_objectives)} objectives, over the "
                       f"{share_cap:.0%} share a plausible misreading usually costs; a narrower wrong "
                       "premise bites harder (measured on the parent's 369 checks)")
            (advisories if policy.MUTANT_SHARE_IS_ADVISORY else errors).append(message)
        if entry["reward"] == 1.0:
            errors.append(f"mutant {mid}: scores full credit")
    for tid, trap in traps.items():
        if not any(m.get("trap") == tid for m in mutants):
            errors.append(f"trap {tid}: has no mutant")
    uncovered = [cid for cid, c in objectives.items() if c.get("band") in policy.MUTANT_COVERAGE_BANDS
                 and cid not in failed_by_some]
    results["coverage"] = {"failed_by_a_mutant": sorted(failed_by_some), "uncovered": uncovered}
    if uncovered:
        errors.append("coverage: no mutant fails these core/stretch objectives, so nothing shows they "
                      "discriminate a plausible wrong fix: " + ", ".join(uncovered))

    # -- multi-turn dependence (Spec Appendix A) -----------------------------
    turns: dict = {}
    for step in steps[1:]:
        label = f"prefix_only:{step}"
        if label in records:
            entry = trial(label)
            flags = entry["flags"].get(step, {})
            passing = sorted(cid for cid, ok in flags.items()
                             if ok and cid in objectives and objectives[cid].get("step") == step)
            turns[label] = {"valid": entry["valid"], "reward": entry["reward"], "passing": passing}
            if entry["valid"] and passing:
                errors.append(f"{label}: with every earlier turn's reference and no new work, step {step} "
                              f"objectives already pass: {', '.join(passing)} -- every turn must need substantive "
                              "new work (Spec Appendix A); a standing rule is re-tested on the turn's NEW path, "
                              "preserved behaviour belongs in a guardrail")
        label = f"skip_prior:{step}"
        if label in records:
            entry = trial(label)
            row = records[label]["steps"].get(step) or {}
            stdout = str((row.get("applied") or {}).get("stdout") or "")
            applied = SKIP_PRIOR_APPLIED in stdout
            step_reward = row.get("reward")
            turns[label] = {"valid": entry["valid"], "applied": applied, "step_reward": step_reward}
            if entry["valid"] and step_reward == 1.0:
                errors.append(f"{label}: step {step}'s reference on the untouched fixture, without the earlier "
                              "turns, scores full credit -- the turn does not build on the state the earlier "
                              "turns created (Spec Appendix A)")
    if turns:
        results["turns"] = turns

    # -- empty, destructive --------------------------------------------------
    empty = trial("empty")
    results["empty"] = empty
    if empty["valid"] and empty["reward"] != 0.0:
        errors.append(f"empty: an emptied working tree scores {empty['reward']}")
    destructive = trial("destructive")
    results["destructive"] = destructive
    if destructive["valid"] and destructive["reward"] != 0.0:
        errors.append(f"destructive: deleting the source scores {destructive['reward']}")

    # -- test tamper ---------------------------------------------------------
    # The exploit to catch is tampering that makes a FAILING test pass (a
    # conftest that skips everything, a config that disables collection, a
    # shadowing package, a pre-written reward). So the planted files go on top
    # of the UNTOUCHED fixture: every objective must still fail. Tampering on
    # top of a correct fix can only lower the score (the agent breaks its own
    # program); that is a model failure, not a grader weakness.
    tampered = trial("test_tamper")
    results["test_tamper"] = tampered
    if tampered["valid"] and nop["valid"] and policy.TEST_TAMPER_MUST_NOT_MOVE_OBJECTIVES:
        for step, flags in tampered["flags"].items():
            passing = [cid for cid, ok in flags.items() if ok and cid in objectives]
            if passing:
                errors.append(f"test_tamper/{step}: objectives pass on the untouched fixture after agent-side "
                              f"tampering: {', '.join(passing)} -- the grader reads agent state")
        if tampered["reward"] not in (0.0, None):
            errors.append(f"test_tamper: reward {tampered['reward']} on a tampered untouched fixture")
    results["tamper_files"] = sorted(TAMPER_FILES)

    # -- early exit ------------------------------------------------------------
    # Reviewer finding (2026-09-27): an `os._exit(0)` in a module the hidden
    # tests import ends the runner with status 0 before any assertion. Planted
    # into EVERY source file of the untouched fixture, it must still leave
    # every objective failing: the grader counts completed tests, and a run
    # that completed none is not a pass.
    exit_shell = EARLY_EXIT_SHELL.get(toolchain.get("name"))
    if exit_shell is None:
        results["early_exit"] = {"valid": False, "skipped": f"no early-exit planting for toolchain "
                                 f"{toolchain.get('name')!r}; declare it an untested surface"}
    else:
        exited = trial("early_exit")
        results["early_exit"] = exited
        if exited["valid"] and nop["valid"]:
            for step, flags in exited["flags"].items():
                passing = [cid for cid, ok in flags.items() if ok and cid in objectives]
                if passing:
                    errors.append(f"early_exit/{step}: objectives pass after every source file exits 0 before "
                                  f"running: {', '.join(passing)} -- the grader trusts an exit status")
            if exited["reward"] not in (0.0, None):
                errors.append(f"early_exit: reward {exited['reward']} after every source file exits 0")

    # -- a benign regression test ----------------------------------------------
    # Reviewer finding G1 (2026-09-30): the introspection preflight refused an
    # honest fix that added an ordinary pytest regression test file. The
    # reference plus such a file must score exactly what the reference scores.
    if toolchain.get("name") == "python":
        benign_run = trial("benign_test")
        results["benign_test"] = benign_run
        if benign_run["valid"] and golden["valid"] and benign_run["reward"] != golden["reward"]:
            errors.append(f"benign_test: the reference plus an ordinary regression test file scores {benign_run['reward']} "
                          f"against the reference's {golden['reward']}; the grader refuses honest engineering")
    else:
        results["benign_test"] = {"valid": False, "skipped": f"no benign-test planting for toolchain {toolchain.get('name')!r}"}

    # -- neutral additions -------------------------------------------------------
    # User, 2026-10-21 (external audit, class 6): a guardrail that fails on a local conftest, a test of
    # the agent's own, notes or a bytecode cache zeroes a correct solution for something outside its
    # sentence (three shipped tasks zeroed their own reference beside a tests/conftest.py). The reference
    # plus those files must read check for check as the reference does, on every step.
    neutral = trial("neutral_additions")
    planted = neutral_additions(bundle, toolchain)
    listed = sorted(planted["files"]) + [d + "/" for d in planted["dirs"]]
    results["neutral_additions"] = {**neutral, "planted": listed, "test_runner": planted["test_runner"]}
    if neutral["valid"] and golden["valid"]:
        misfires, budgets = neutral_moves(doc, golden["flags"], neutral["flags"])
        by_id = {c["id"]: c for c in doc["checks"]}
        for step, ids in budgets.items():
            advisories.append(f"neutral_additions/{step}: {', '.join(ids)} counts the harmless files an honest agent "
                              f"adds ({', '.join(listed)}) against its file budget; right only when the instruction "
                              "publishes that budget, and its rule_quote must say so")
        for step, ids in misfires.items():
            why = []
            for cid in ids:
                if shadow_conftest_problems([by_id[cid]]):
                    why.append(f"{cid} is a shadow-guard listing `conftest`, which turns off grade.py's exemption "
                               "for an agent-added conftest.py")
                elif by_id[cid]["kind"] == "objective":
                    why.append(f"{cid} is an objective that grades the tree's inventory, not the behaviour its "
                               "sentence states")
                else:
                    why.append(f"{cid} is a guardrail failing on files outside its sentence")
            message = (f"neutral_additions/{step}: the reference plus what an honest agent commonly adds "
                       f"({', '.join(listed)}) fails {', '.join(ids)}; " + "; ".join(why) + " -- a correct "
                       "solution that keeps a local conftest, its own test, notes or a bytecode cache must not "
                       "lose credit")
            (errors if policy.NEUTRAL_ADDITIONS_MUST_MATCH_REFERENCE else advisories).append(message)
        if not misfires and not budgets and neutral["reward"] != golden["reward"]:
            contract = {s: e for s, e in neutral["contract_errors"].items() if e}
            (errors if policy.NEUTRAL_ADDITIONS_MUST_MATCH_REFERENCE else advisories).append(
                f"neutral_additions: the reference plus harmless additions scores {neutral['reward']} against the "
                f"reference's {golden['reward']} with every check unmoved; contract errors: {contract}")

    # -- report forgery from inside the test process ---------------------------
    # Reviewer finding S1 (2026-09-30), reproduced on a validated task: code
    # the hidden tests import ran inside the grader's interpreter and read
    # the run's report path and nonce from `__main__`. Planted into EVERY
    # source file of the untouched fixture, the hunt below must find nothing
    # it can forge with, so every objective still fails.
    forge_shell = REPORT_FORGE_SHELL.get(toolchain.get("name"))
    if forge_shell is None:
        results["report_forge"] = {"valid": False, "skipped": f"no report-forgery planting for toolchain "
                                   f"{toolchain.get('name')!r}; declare it an untested surface"}
    else:
        forged = trial("report_forge")
        results["report_forge"] = forged
        if forged["valid"] and nop["valid"]:
            for step, flags in forged["flags"].items():
                passing = [cid for cid, ok in flags.items() if ok and cid in objectives]
                if passing:
                    errors.append(f"report_forge/{step}: objectives pass after every source file hunts the "
                                  f"grader's report identity and forges a report: {', '.join(passing)} -- the "
                                  "test process leaks its report path or nonce")
            if forged["reward"] not in (0.0, None):
                errors.append(f"report_forge: reward {forged['reward']} after every source file forges the report")

    return results, errors, advisories


def _cache_shape(doc: dict, steps: list[str]) -> dict:
    return {step: [c["id"] for c in doc["checks"] if c.get("step") == step] for step in steps}


def default_workers() -> int:
    """Trials one control run executes at once: `ARENA_CONTROL_WORKERS`, else 1.

    Serial controls were a quarter of all design time on 2026-10-03 (median 9
    minutes a run, 5.5 runs a design) while the Coder workspaces idled at a
    load of 3-20 on 32 cores; the Coder runs set 3.
    """
    import os
    raw = os.environ.get("ARENA_CONTROL_WORKERS", "1")
    return int(raw) if raw.isdigit() else 1


def run(bundle: Path, *, work: Path | None = None, determinism_runs: int | None = None,
        workers: int | None = None, cache_root: Path | None = None, reuse: bool = True) -> dict:
    """Execute controls or bind complete equivalent execution evidence to export.

    Name and provenance changes reuse executed trials; final provenance/gates
    and semantic review still run. Set reuse=False for an intentional fresh run.
    """
    if workers is None:
        workers = default_workers()
    if type(workers) is not int or not 1 <= workers <= policy.MAX_CONTROL_WORKERS:
        raise ValueError(f"control workers must be 1..{policy.MAX_CONTROL_WORKERS}")
    bundle = Path(bundle).resolve()
    spec = load_spec(bundle)
    doc = load_shipped(bundle / "tests/checks.json")
    steps = list(json.loads((bundle / "tests/rubric.json").read_text()))
    toolchain = sandbox._toolchain(bundle)
    runs = determinism_runs if determinism_runs is not None else policy.DETERMINISM_RUNS
    plan = _trial_plan(bundle, spec, doc, steps, toolchain, runs)
    started = time.time()
    inputs = control_cache.snapshot(bundle, runs)
    cache_root = Path(cache_root or bundle / control_cache.ROOT).resolve()
    if reuse:
        for path in control_cache.candidates(cache_root, inputs):
            try:
                evidence = control_cache.read(path, inputs, plan, steps, _cache_shape(doc, steps))
                results, errors, advisories = _evaluate(bundle, spec, doc, steps, toolchain, runs,
                                                        evidence["records"])
                source = evidence["receipt"]
                if errors or any(source.get(k) != v for k, v in
                                 (("results", results), ("errors", errors), ("advisories", advisories))):
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                continue
            # Copy evidence into the published bundle so certification never
            # depends on a scratch directory or a different bundle's lifetime.
            local, sha = control_cache.retain(bundle / control_cache.ROOT, evidence["inputs"],
                                             source, evidence["records"])
            receipt = {**source, "artifact_sha256": inputs["artifact_sha256"],
                       "seconds": round(time.time() - started, 3),
                       "execution": {**source["execution"], "reused": True, "executed_new_trials": 0,
                           "source_artifact_sha256": source["artifact_sha256"],
                           "source_seconds": source["seconds"],
                           "evidence_path": local.relative_to(bundle).as_posix(), "evidence_sha256": sha,
                           "metadata_changes": control_cache.changed_metadata(evidence["inputs"], inputs)}}
            if control_cache.snapshot(bundle, runs) != inputs:
                raise sandbox.SandboxError("bundle changed or policy changed while reusing controls; no receipt written")
            atomic_json(bundle / RECEIPT, receipt)
            return receipt

    work = Path(work or bundle / "verification/controls-work")
    if work.exists():
        import shutil
        shutil.rmtree(work)
    work.mkdir(parents=True)
    agent_tag = sandbox.build_image(bundle, "agent", log=work / "build-agent.log")
    verifier_tag = sandbox.build_image(bundle, "verifier", log=work / "build-verifier.log")
    try:
        records = _execute_trials(bundle, plan, work, agent_tag, verifier_tag, workers)
    except _ControlPreflightFailure as exc:
        # Keep the genuine partial trials for diagnosis. They are neither a
        # full control receipt nor reusable execution evidence.
        atomic_json(work / "control-preflight-failure.json", {
            "schema": "arena-coding/control-preflight-failure/1",
            "artifact_sha256": inputs["artifact_sha256"],
            "input_binding": control_cache.binding(inputs),
            "images": {"agent": agent_tag, "verifier": verifier_tag},
            "errors": exc.errors, "records": exc.records,
            "execution": {"workers": workers, "executed_new_trials": len(exc.records),
                          "planned_trials": len(plan), "complete": False},
            "ok": False, "reusable": False})
        raise
    results, errors, advisories = _evaluate(bundle, spec, doc, steps, toolchain, runs, records)
    if control_cache.snapshot(bundle, runs) != inputs:
        raise sandbox.SandboxError("bundle changed or policy changed during controls; no receipt written")
    receipt = {"schema": SCHEMA, "artifact_sha256": inputs["artifact_sha256"],
               "input_binding": control_cache.binding(inputs),
               "images": {"agent": agent_tag, "verifier": verifier_tag},
               "steps": steps, "results": results, "errors": errors, "advisories": advisories, "ok": not errors,
               "seconds": round(time.time() - started, 1),
               "execution": {"workers": workers, "reused": False, "executed_new_trials": len(records)},
               "policy": {"determinism_runs": runs,
                   "misreading_max_objective_share": policy.MISREADING_MAX_OBJECTIVE_SHARE,
                   "mutant_share_is_advisory": policy.MUTANT_SHARE_IS_ADVISORY,
                   "test_tamper_must_not_move_objectives": policy.TEST_TAMPER_MUST_NOT_MOVE_OBJECTIVES,
                   "coverage_bands": list(policy.MUTANT_COVERAGE_BANDS),
                   "neutral_additions_must_match_reference": policy.NEUTRAL_ADDITIONS_MUST_MATCH_REFERENCE,
                   "neutral_additions_exempt_runners": list(policy.NEUTRAL_ADDITIONS_EXEMPT_RUNNERS)}}
    path, sha = control_cache.retain(cache_root, inputs, receipt, records)
    if cache_root != bundle / control_cache.ROOT:
        path, sha = control_cache.retain(bundle / control_cache.ROOT, inputs, receipt, records)
    receipt["execution"] = {**receipt["execution"], "evidence_path": path.relative_to(bundle).as_posix(),
                            "evidence_sha256": sha, "source_artifact_sha256": inputs["artifact_sha256"]}
    atomic_json(bundle / RECEIPT, receipt)
    return receipt


def binding_problems(bundle: Path, receipt: dict) -> list[str]:
    """New receipts require intact execution evidence, including after a rename.

    Historical exact-artifact receipts remain supported; summary-only historical
    receipts cannot be migrated to another artifact by the cache resolver.
    """
    if not receipt.get("input_binding"):
        return []
    try:
        runs = receipt["input_binding"]["determinism_runs"]
        inputs = control_cache.snapshot(bundle, runs)
        if receipt["input_binding"] != control_cache.binding(inputs):
            return ["controls execution inputs or policy changed; re-run the controls"]
        execution = receipt["execution"]
        path = contained(bundle, execution["evidence_path"])
        if path.stem != execution["evidence_sha256"]:
            return ["controls evidence reference differs"]
        spec = load_spec(bundle)
        doc = load_shipped(bundle / "tests/checks.json")
        steps = list(json.loads((bundle / "tests/rubric.json").read_text()))
        chain = sandbox._toolchain(bundle)
        plan = _trial_plan(bundle, spec, doc, steps, chain, runs)
        evidence = control_cache.read(path, inputs, plan, steps, _cache_shape(doc, steps))
        results, errors, advisories = _evaluate(bundle, spec, doc, steps, chain, runs, evidence["records"])
        source = evidence["receipt"]
        if (source["artifact_sha256"] != execution["source_artifact_sha256"]
                or any(receipt.get(k) != v or source.get(k) != v for k, v in
                       (("results", results), ("errors", errors), ("advisories", advisories)))
                or any(receipt.get(k) != source.get(k) for k in ("steps", "images", "policy", "ok"))):
            return ["controls receipt differs from its executed evidence"]
        if not execution.get("reused") and source["artifact_sha256"] != inputs["artifact_sha256"]:
            return ["controls receipt misstates the executed artifact"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"controls execution evidence is invalid: {exc}"]
    return []


def problems(bundle: Path) -> list[str]:
    """Why the retained controls receipt does not certify this bundle version."""
    bundle = Path(bundle)
    path = bundle / RECEIPT
    if not path.is_file():
        return ["controls have not been executed (verification/controls.json is missing)"]
    try:
        receipt = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return [f"controls receipt unreadable: {exc}"]
    if receipt.get("schema") != SCHEMA:
        return [f"controls receipt is not {SCHEMA}"]
    if receipt.get("artifact_sha256") != identity(bundle):
        return ["controls receipt is for another version of this bundle; re-run the controls"]
    if not receipt.get("ok"):
        return list(receipt.get("errors") or ["controls were unsuccessful"])
    if issues := binding_problems(bundle, receipt):
        return issues
    return []
