"""What the built agent image can actually run: shells, interpreters, imports, packages.

User, 2026-10-07: "All the tasks should be built with complete packages and all the
necessary imports and ensure none of the packages are missing, and Arena team should
be able to reproduce the env without any issues."

A manifest comparison (`shipcheck.deps_declared`) only sees what a repository
declares. This probe runs INSIDE the agent image, as the agent, in /app/repo, and asks
the image itself:

    WHICH       each toolchain binary under sh -c, sh -lc, bash -c and bash -lc
    QUIET       each interpreter starts with nothing on stderr
    PYIMPORT    a module the repository's Python imports that does not resolve
    NODEIMPORT  a bare package the repository's JavaScript requires/imports that no
                node_modules on its path holds
    GOIMPORT    a Go module whose packages `go list -deps` cannot load offline
    COMPOSER    a composer.json requirement with no vendor/ to satisfy it
    PIPCHECK    an installed Python distribution whose requirements are broken

Imports a file guards (inside `try: ... except ImportError`, under `if TYPE_CHECKING:`
or a platform test) are optional by the code's own account and are not reported.
Every line the probe prints is a fact about the image; `shipcheck` turns lines into
findings and a human waiver is the only way past one that cannot be installed.
"""
from __future__ import annotations

PY_IMPORTS = r'''
import ast, os, sys, importlib.util
ROOT = "/app/repo"
SKIP = {"node_modules", ".git", "venv", ".venv", "__pycache__", "site-packages", "dist", "build"}
OPTIONAL = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
files, local = [], {"__future__", "__main__"}
for dp, dns, fns in os.walk(ROOT):
    dns[:] = [d for d in dns if d not in SKIP and not d.startswith(".")]
    if "__init__.py" in fns:
        local.add(os.path.basename(dp))
    local.update(d for d in dns)
    local.update(d.replace("-", "_") for d in dns)   # plugin loaders import plugins/polya-heuristic as polya_heuristic
    for f in fns:
        if f.endswith(".py"):
            files.append(os.path.join(dp, f))
            local.add(f[:-3])
std = set(getattr(sys, "stdlib_module_names", ())) | set(sys.builtin_module_names)
def guarded(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            names = set()
            for h in node.handlers:
                t = h.type
                for e in (t.elts if isinstance(t, ast.Tuple) else [t]):
                    names.add(getattr(e, "id", None) or getattr(e, "attr", None) if e is not None else "BaseException")
            if names & OPTIONAL or any(h.type is None for h in node.handlers):
                for sub in node.body:
                    out.update(id(n) for n in ast.walk(sub))
        elif isinstance(node, ast.If):
            test = ast.unparse(node.test) if hasattr(ast, "unparse") else ""
            if any(k in test for k in ("TYPE_CHECKING", "platform", "os.name", "sys.version")):
                for sub in node.body + node.orelse:
                    out.update(id(n) for n in ast.walk(sub))
    return out
missing = {}
for p in files:
    try:
        tree = ast.parse(open(p, encoding="utf-8", errors="replace").read())
    except (SyntaxError, ValueError):
        continue
    skip = guarded(tree)
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            if top in std or top in local:
                continue
            try:
                found = importlib.util.find_spec(top) is not None
            except (ImportError, ValueError):
                found = False
            if not found:
                missing.setdefault(top, set()).add(os.path.relpath(p, ROOT))
for k in sorted(missing):
    print("PYIMPORT|%s|%s" % (k, ",".join(sorted(missing[k])[:5])))
'''

NODE_IMPORTS = r'''
const fs = require("fs"), path = require("path"), { builtinModules } = require("module");
const ROOT = "/app/repo";
const SKIP = new Set(["node_modules", ".git", "dist", "build", ".next", "coverage", "vendor"]);
const builtins = new Set(builtinModules);
const RE = /(?:\brequire\(\s*|\bfrom\s+|\bimport\s*\(\s*|^\s*import\s+)['"]([^'"\n]+)['"]/gm;
const miss = {};
function has(dir, name) {
  for (let d = dir; d.startsWith(ROOT) || d === "/"; d = path.dirname(d)) {
    if (fs.existsSync(path.join(d, "node_modules", name, "package.json"))) return true;
    if (d === "/" || d === path.dirname(d)) break;
  }
  return ["/opt/deps/node_modules", "/usr/local/lib/node_modules"].some(g => fs.existsSync(path.join(g, name, "package.json")));
}
// The innermost block around the import is a `try` block: an optional package the code itself
// catches (`try { return require('canvas'); } catch {}`), as the Python scan honours
// `except ImportError` (c-006-node-weather-backend, 2026-10-14).
function guarded(text, at) {
  let depth = 0;
  for (let i = at - 1; i >= 0; i--) {
    if (text[i] === "}") depth++;
    else if (text[i] === "{") {
      if (depth === 0) return /\btry\s*$/.test(text.slice(Math.max(0, i - 16), i));
      depth--;
    }
  }
  return false;
}
function scan(p) {
  const text = fs.readFileSync(p, "utf8").split("\n").filter(l => !/^\s*(\/\/|\*)/.test(l)).join("\n");
  let m;
  while ((m = RE.exec(text))) {
    const s = m[1];
    if (/^(\.|\/|node:|https?:|data:|#)/.test(s) || s.includes("${") || guarded(text, m.index)) continue;
    const name = s.startsWith("@") ? s.split("/").slice(0, 2).join("/") : s.split("/")[0];
    if (builtins.has(name) || !/^(@[a-z0-9._-]+\/)?[a-z0-9._-]+$/i.test(name)) continue;
    if (!has(path.dirname(p), name)) (miss[name] = miss[name] || new Set()).add(path.relative(ROOT, p));
  }
}
(function walk(d) {
  for (const e of fs.readdirSync(d, { withFileTypes: true })) {
    if (SKIP.has(e.name) || e.name.startsWith(".")) continue;
    const p = path.join(d, e.name);
    if (e.isDirectory()) walk(p);
    else if (/\.(c|m)?[jt]sx?$/.test(e.name) && !/\.d\.ts$/.test(e.name)) scan(p);
  }
})(ROOT);
for (const k of Object.keys(miss).sort()) console.log("NODEIMPORT|" + k + "|" + [...miss[k]].slice(0, 5).join(","));
'''

SHELL = r'''
cd /app/repo
for s in "sh -c" "sh -lc" "bash -c" "bash -lc"; do
  for b in python python3 node npm go cargo php; do printf 'WHICH|%s|%s|%s\n' "$s" "$b" "$($s "command -v $b" 2>/dev/null)"; done
  for b in python3 python php node; do
    if $s "command -v $b" >/dev/null 2>&1; then
      # The command is re-parsed by $s: php's code must be quoted or its ';' ends the
      # command and php starts with a bare -r (a false noisy start on c-008-php, 2026-10-07).
      case $b in node) a="-e 0";; php) a="-r 'exit(0);'";; *) a="-c pass";; esac
      printf 'QUIET|%s|%s|%s\n' "$s" "$b" "$($s "$b $a" 2>&1 >/dev/null | head -c 400 | tr '\n' ' ')"
    fi
  done
done
PY=$(command -v python3 || command -v python)
if [ -n "$PY" ] && find /app/repo -name '*.py' -not -path '*/node_modules/*' | head -1 | grep -q .; then
  cat > /tmp/arena-pyimports.py <<'ARENA_PY_EOF'
@PY_IMPORTS@
ARENA_PY_EOF
  "$PY" /tmp/arena-pyimports.py 2>/dev/null
fi
if [ -x /opt/deps/venv/bin/python ]; then
  /opt/deps/venv/bin/python -m pip check 2>/dev/null | grep -vi '^no broken requirements' | sed 's/^/PIPCHECK|/'
fi
if command -v node >/dev/null 2>&1 && find /app/repo \( -name '*.js' -o -name '*.mjs' -o -name '*.cjs' -o -name '*.ts' -o -name '*.jsx' -o -name '*.tsx' \) -not -path '*/node_modules/*' | head -1 | grep -q .; then
  cat > /tmp/arena-nodeimports.js <<'ARENA_JS_EOF'
@NODE_IMPORTS@
ARENA_JS_EOF
  node /tmp/arena-nodeimports.js 2>/dev/null
fi
if command -v go >/dev/null 2>&1; then
  for m in $(find /app/repo -name go.mod -not -path '*/vendor/*' 2>/dev/null); do
    d=$(dirname "$m")
    out=$(cd "$d" && GOPROXY=off GOFLAGS=-mod=mod go list -deps ./... 2>&1 >/dev/null | grep -E 'cannot find module|no required module|missing go.sum|cannot find package|module lookup disabled' | head -3 | tr '\n' ' ')
    [ -n "$out" ] && printf 'GOIMPORT|%s|%s\n' "${d#/app/repo/}" "$out"
  done
fi
for c in $(find /app/repo -name composer.json -not -path '*/vendor/*' 2>/dev/null); do
  d=$(dirname "$c")
  if [ ! -f "$d/vendor/autoload.php" ]; then
    php -r '$j=json_decode(file_get_contents($argv[1]),true); foreach(array_keys($j["require"]??[]) as $k){ if($k!=="php" && strpos($k,"ext-")!==0) echo "COMPOSER|$k|".$argv[2]."\n"; }' "$c" "${d#/app/repo/}" 2>/dev/null
  fi
done
true
'''


def script() -> str:
    return SHELL.replace("@PY_IMPORTS@", PY_IMPORTS.strip()).replace("@NODE_IMPORTS@", NODE_IMPORTS.strip())


def parse(text: str) -> dict:
    out = {"which": {}, "quiet": {}, "py": {}, "node": {}, "go": {}, "composer": {}, "pip": []}
    for line in (text or "").splitlines():
        parts = line.split("|", 3)
        tag = parts[0]
        if tag == "WHICH" and len(parts) == 4:
            out["which"].setdefault(parts[2], {})[parts[1]] = parts[3].strip()
        elif tag == "QUIET" and len(parts) == 4:
            out["quiet"].setdefault(parts[2], {})[parts[1]] = parts[3].strip()
        elif tag in ("PYIMPORT", "NODEIMPORT", "GOIMPORT", "COMPOSER") and len(parts) >= 3:
            key = {"PYIMPORT": "py", "NODEIMPORT": "node", "GOIMPORT": "go", "COMPOSER": "composer"}[tag]
            out[key][parts[1]] = parts[2].strip()
        elif tag == "PIPCHECK" and len(parts) >= 2:
            out["pip"].append(line.split("|", 1)[1].strip())
    return out
