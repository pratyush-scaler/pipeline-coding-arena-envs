"""Toolchains: the container a coding task runs in, generated from one record.

A bundle's `environment/toolchain.json` names a toolchain and its dependency
manifests. From that ONE record this module writes both Dockerfiles -- the
agent image (`environment/Dockerfile`) and the offline verifier image
(`tests/Dockerfile`) -- so the two cannot drift: same digest-pinned base, same
Debian snapshot, same dependency install. The verifier additionally carries
the hidden tests and the test-only dependencies; the agent image never sees
either (Harbor's `tests_or_solution_in_image` and `test_deps_in_image`).

Dependencies live OUTSIDE the working tree, under /opt/deps, so the agent's
`/app/repo` capture (which excludes them) restores cleanly in the verifier and
the shipped grader re-links them (`grade.restore_tree`). The pristine repo is
copied into the image at build time; nothing is fetched at run time -- the
agent phase reaches only its model API host and the verifier nothing.

Every base image is a multi-platform index digest (resolved 2026-09-27 with
`docker buildx imagetools inspect`), so the same Dockerfile builds on the
arm64 dev box and the amd64 Coder pool. Changing a pin is an explicit version
change that invalidates every measurement through the artifact hash.
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA = "arena-coding/toolchain/1"
DEPS_ROOT = "/opt/deps"
REPO = "/app/repo"
DELIVERABLES = "/app/deliverables"

#: The Debian snapshot every bookworm-based image installs from. Shared with
#: the parent pipeline's harness layer so both programmes build the same OS.
APT_SNAPSHOT = "https://snapshot.debian.org/archive/debian/20260919T000000Z"

#: Multi-platform index digests.
IMAGES = {
    "python": "python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e",
    "node": "node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c",
    "go": "golang:1.23-bookworm@sha256:167053a2bb901972bf2c1611f8f52c44d5fe7e762e5cab213708d82c421614db",
    "rust": "rust:1.83-slim-bookworm@sha256:540c902e99c384163b688bbd8b5b8520e94e7731b27f7bd0eaa56ae1960627ab",
    "php": "php:8.3-cli-bookworm@sha256:4687aec76c4a895b68b91bcd5e48f1ba7a3dea120f6de50bba7e1a93af5372dd",
    # Ubuntu noble, ships Node 22 and Chromium. Verifier images only.
    "playwright": "mcr.microsoft.com/playwright:v1.49.1-noble@sha256:70e367e0cbf60340a5b5fd562f6247a34eb3196efab9f88a3dd56482d9fe09d2",
}

#: Packages every agent image carries: the harness CLIs' needs (tmux, rg,
#: procps), git and patch for the reference solution, python3 for the grader.
COMMON_APT = ("tmux=3.3a-3", "ripgrep", "procps", "ca-certificates", "git", "patch")

#: Pinned test-only dependencies per toolchain, installed in the VERIFIER only.
TEST_DEPS = {
    "python": ["pytest==8.3.4", "pytest-timeout==2.3.1"],
    "node": {"jest": "29.7.0", "vitest": "2.1.8"},
    "browser": {"@playwright/test": "1.49.1"},
}


class Toolchain:
    """One language's container recipe. Data, with generation methods."""

    def __init__(self, name: str, *, base: str, apt: tuple[str, ...] = (), needs_node: bool,
                 needs_python: bool, install: str = "", verifier_install: str = "",
                 vendored: tuple[str, ...] = (), env: dict | None = None,
                 manifests: tuple[str, ...] = (), verifier_base: str | None = None,
                 verifier_apt: tuple[str, ...] = (), runners: tuple[str, ...] = ()):
        self.name = name
        self.base = base
        self.apt = apt
        self.needs_node = needs_node
        self.needs_python = needs_python
        self.install = install
        self.verifier_install = verifier_install
        self.vendored = vendored
        self.env = env or {}
        self.manifests = manifests
        self.verifier_base = verifier_base or base
        self.verifier_apt = verifier_apt
        self.runners = runners


_PY_INSTALL = (
    "python3 -m venv /opt/deps/venv && /opt/deps/venv/bin/pip install --no-cache-dir "
    "--disable-pip-version-check -r /opt/deps/manifest/requirements.txt")
_PY_VERIFIER = (
    "python3 -m venv /opt/deps/venv && /opt/deps/venv/bin/pip install --no-cache-dir "
    "--disable-pip-version-check -r /opt/deps/manifest/requirements.txt "
    "-r /opt/deps/manifest/requirements-test.txt")
_NODE_INSTALL = (
    "cd /opt/deps/manifest && npm ci --no-audit --no-fund --ignore-scripts && "
    "mv node_modules /opt/deps/node_modules && npm cache clean --force")

# The verifier manifest adds pinned test dependencies to its private copy.
_NODE_VERIFIER_INSTALL = _NODE_INSTALL.replace(
    "npm ci", "npm install --package-lock-only --no-audit --no-fund --ignore-scripts && npm ci", 1)

TOOLCHAINS: dict[str, Toolchain] = {
    "python": Toolchain(
        "python", base=IMAGES["python"], needs_node=True, needs_python=False,
        install=_PY_INSTALL, verifier_install=_PY_VERIFIER,
        env={"VIRTUAL_ENV": "/opt/deps/venv", "PATH": "/opt/deps/venv/bin:/usr/local/bin:/usr/bin:/bin"},
        manifests=("requirements.txt",), runners=("pytest", "unittest", "command")),
    "node": Toolchain(
        "node", base=IMAGES["node"], apt=("python3",), needs_node=False, needs_python=True,
        install=_NODE_INSTALL, verifier_install=_NODE_VERIFIER_INSTALL, vendored=("node_modules",),
        manifests=("package.json", "package-lock.json"),
        runners=("node-test", "jest", "vitest", "mocha", "command")),
    "browser": Toolchain(
        # Single-file or static web apps: the agent gets Node and a static
        # server; behaviour is graded headless in a Playwright verifier.
        "browser", base=IMAGES["node"], apt=("python3",), needs_node=False, needs_python=True,
        install=_NODE_INSTALL, verifier_install=_NODE_VERIFIER_INSTALL, vendored=("node_modules",),
        manifests=("package.json", "package-lock.json"),
        verifier_base=IMAGES["playwright"], runners=("playwright", "node-test", "command")),
    "go": Toolchain(
        "go", base=IMAGES["go"], apt=("python3",), needs_node=True, needs_python=True,
        install="cd /opt/deps/manifest && GOMODCACHE=/opt/deps/gomod go mod download",
        verifier_install="cd /opt/deps/manifest && GOMODCACHE=/opt/deps/gomod go mod download",
        env={"GOMODCACHE": "/opt/deps/gomod", "GOFLAGS": "-mod=mod", "GOPROXY": "off",
             "GOCACHE": "/tmp/gocache"},
        manifests=("go.mod", "go.sum"), runners=("go-test", "command")),
    "rust": Toolchain(
        "rust", base=IMAGES["rust"], apt=("python3", "gcc", "libc6-dev", "pkg-config"),
        needs_node=True, needs_python=True,
        install="cd /opt/deps/manifest && CARGO_HOME=/opt/deps/cargo cargo fetch",
        verifier_install="cd /opt/deps/manifest && CARGO_HOME=/opt/deps/cargo cargo fetch",
        env={"CARGO_HOME": "/opt/deps/cargo", "CARGO_TARGET_DIR": "/tmp/cargo-target",
             "CARGO_NET_OFFLINE": "true"},
        manifests=("Cargo.toml", "Cargo.lock"), runners=("cargo", "command")),
    "php": Toolchain(
        "php", base=IMAGES["php"], apt=("python3",), needs_node=True, needs_python=True,
        verifier_install="python3 -m venv /opt/deps/venv && /opt/deps/venv/bin/pip install "
                         "--no-cache-dir --disable-pip-version-check -r /opt/deps/manifest/requirements-test.txt",
        env={"PATH": "/opt/deps/venv/bin:/usr/local/bin:/usr/bin:/bin"},
        manifests=(), runners=("pytest", "command")),
    "c": Toolchain(
        "c", base=IMAGES["python"], apt=("gcc", "g++", "make", "cmake", "libc6-dev"),
        needs_node=True, needs_python=False,
        verifier_install="python3 -m venv /opt/deps/venv && /opt/deps/venv/bin/pip install "
                         "--no-cache-dir --disable-pip-version-check -r /opt/deps/manifest/requirements-test.txt",
        env={"PATH": "/opt/deps/venv/bin:/usr/local/bin:/usr/bin:/bin"},
        manifests=(), runners=("pytest", "command")),
    "shell": Toolchain(
        "shell", base=IMAGES["python"], apt=("bash", "coreutils", "jq"), needs_node=True,
        needs_python=False,
        verifier_install="python3 -m venv /opt/deps/venv && /opt/deps/venv/bin/pip install "
                         "--no-cache-dir --disable-pip-version-check -r /opt/deps/manifest/requirements-test.txt",
        env={"PATH": "/opt/deps/venv/bin:/usr/local/bin:/usr/bin:/bin"},
        manifests=(), runners=("pytest", "command")),
}

#: Languages the corpus carries that no toolchain here can run. A seed in one
#: of these is `no_runtime` at selection unless it can be cut to pure logic.
NO_RUNTIME = ("roblox", "gmod", "unity", "android", "flutter", "mql5", "hdl", "gdscript", "swift")


def load(bundle: Path) -> dict:
    path = Path(bundle) / "environment/toolchain.json"
    doc = json.loads(path.read_text())
    if doc.get("schema") != SCHEMA:
        raise ValueError(f"{path} is not a {SCHEMA} document")
    if doc.get("name") not in TOOLCHAINS:
        raise ValueError(f"unknown toolchain {doc.get('name')!r}")
    return doc


def record(name: str, *, language: str, extra_apt: list[str] | None = None,
           post_install: list[str] | None = None, verifier_timeout_s: int = 1800,
           agent_timeout_s: int = 10800, cpus: int = 2, memory_mb: int = 4096,
           login_shell_path: bool = False) -> dict:
    """The toolchain.json document a bundle carries.

    `login_shell_path` is written only when true, so a record staged before
    the key existed generates the same bytes it always did.
    """
    chain = TOOLCHAINS[name]
    doc = {"schema": SCHEMA, "name": name, "language": language,
            "base_image": chain.base, "verifier_base_image": chain.verifier_base,
            "apt_snapshot": APT_SNAPSHOT, "apt": sorted(set(chain.apt) | set(extra_apt or [])),
            "vendored": list(chain.vendored), "deps_root": DEPS_ROOT,
            "env": dict(chain.env), "manifests": list(chain.manifests),
            "post_install": list(post_install or []), "runners": list(chain.runners),
            "test_deps": TEST_DEPS.get(name, []),
            "limits": {"verifier_timeout_s": verifier_timeout_s, "agent_timeout_s": agent_timeout_s,
                       "cpus": cpus, "memory_mb": memory_mb}}
    if login_shell_path:
        doc["login_shell_path"] = True
    return doc


#: Codex runs every command as `/bin/sh -lc`, and a login shell sources
#: Debian's /etc/profile, whose first act is `PATH="/usr/local/bin:/usr/bin:..."`.
#: That discards the image's ENV PATH: the venv (python), /usr/local/go/bin
#: (go) and /usr/local/cargo/bin (rust) vanish. Arena's review (C-002, C-032,
#: 2026-10-05) and our own runs (5/5 Astra episodes on anki, llm-client and
#: go-desktop) hit it; Claude Code's non-login shell never does. /etc/profile
#: then sources /etc/profile.d/*.sh, so re-exporting the image's own PATH
#: there makes `sh -c`, `sh -lc`, `bash -c` and `bash -lc` resolve the same
#: binaries.
LOGIN_SHELL_PATH_LAYER = ("RUN printf 'export PATH=\"%s\"\\n' \"$PATH\" > /etc/profile.d/00-arena-path.sh && "
                          "chmod 0644 /etc/profile.d/00-arena-path.sh\n")


def runtime_view(doc: dict) -> dict:
    """The part of the record the shipped grader needs (embedded in checks.json)."""
    return {"name": doc["name"], "vendored": list(doc.get("vendored", [])),
            "deps_root": doc.get("deps_root", DEPS_ROOT), "env": dict(doc.get("env", {}))}


# ---------------------------------------------------------------------------
# Dockerfiles
# ---------------------------------------------------------------------------

def _apt_layer(snapshot: str, packages: list[str], *, bookworm: bool) -> str:
    if not bookworm or not packages:
        return ""
    return ("RUN rm -f /etc/apt/sources.list.d/debian.sources && "
            f"printf 'deb [check-valid-until=no] {snapshot} bookworm main\\n' > /etc/apt/sources.list && "
            "apt-get -o Acquire::Check-Valid-Until=false update && "
            "apt-get install -y --no-install-recommends " + " ".join(packages) + " && "
            "rm -rf /var/lib/apt/lists/*\n")


def _certificate_bootstrap(base: str) -> tuple[str, str]:
    # Node's pinned slim image has no CA bundle, so it cannot reach the HTTPS
    # snapshot that installs ca-certificates. Borrow the trust store from our
    # existing pinned Python image before apt; apt then installs its own bundle.
    if base != IMAGES["node"]:
        return "", ""
    return (f"FROM {IMAGES['python']} AS certificates\n",
            "COPY --from=certificates /etc/ssl/certs/ca-certificates.crt "
            "/etc/ssl/certs/ca-certificates.crt\n")


def _harness_layer(chain: Toolchain, versions: dict) -> str:
    lines = ""
    if chain.needs_node:
        lines += ("COPY --from=node /usr/local/bin/node /usr/local/bin/node\n"
                  "COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm\n"
                  "RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && "
                  "ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx\n")
    lines += ("RUN npm install -g --no-audit --no-fund "
              f"@anthropic-ai/claude-code@{versions['claude-code']} @openai/codex@{versions['codex']} && "
              "npm cache clean --force && claude --version && codex --version\n")
    return lines


def agent_dockerfile(doc: dict) -> str:
    """The agent image: base, snapshot apt, harness CLIs, deps, the repo."""
    from .. import policy
    chain = TOOLCHAINS[doc["name"]]
    bookworm = "bookworm" in chain.base
    text, certificates = _certificate_bootstrap(chain.base)
    if chain.needs_node:
        text += f"FROM {IMAGES['node']} AS node\n"
    text += f"FROM {chain.base}\n"
    text += certificates
    packages = list(COMMON_APT) + list(doc.get("apt", []))
    text += _apt_layer(doc.get("apt_snapshot", APT_SNAPSHOT), packages, bookworm=bookworm)
    text += _harness_layer(chain, policy.HARNESS_VERSIONS)
    if chain.base == IMAGES["node"]:
        # The pinned Node image already reserves UID/GID 1000 for node.
        text += ("RUN usermod --login agent --home /home/agent --move-home node && "
                 "groupmod --new-name agent node\n")
    else:
        text += "RUN useradd --create-home --uid 1000 agent\n"
    if doc.get("manifests"):
        text += "COPY deps/ /opt/deps/manifest/\n"
    if chain.install:
        text += f"RUN mkdir -p /opt/deps && {chain.install}\n"
    for step in doc.get("post_install", []):
        text += f"RUN {step}\n"
    # Sorted: the record and the re-loaded toolchain.json must generate the
    # same bytes whatever order their keys were written in.
    for key, value in sorted(doc.get("env", {}).items()):
        text += f"ENV {key}={value}\n"
    if doc.get("login_shell_path"):
        text += LOGIN_SHELL_PATH_LAYER
    text += f"WORKDIR /app\nCOPY repo/ {REPO}/\n"
    links = " && ".join(f"ln -sfn {DEPS_ROOT}/{Path(v).name} {REPO}/{v}" for v in doc.get("vendored", []))
    text += ("RUN " + (links + " && " if links else "") +
             f"mkdir -p {DELIVERABLES} && chown -R agent:agent {REPO} {DELIVERABLES} && "
             f"chmod -R a-w {DEPS_ROOT}\n")
    text += f"USER agent\nWORKDIR {REPO}\nCMD [\"sleep\", \"infinity\"]\n"
    return text


def verifier_dockerfile(doc: dict) -> str:
    """The verifier image: same base and deps, plus hidden tests and test deps."""
    chain = TOOLCHAINS[doc["name"]]
    base = doc.get("verifier_base_image", chain.verifier_base)
    bookworm = "bookworm" in base
    text, certificates = _certificate_bootstrap(base)
    text += f"FROM {base}\n"
    text += certificates
    packages = ["ca-certificates", "git", "patch"] + list(doc.get("apt", []))
    if chain.needs_python and "python3" not in packages:
        packages.append("python3")
    # A Debian-provided python3 (non-python bases) needs its venv module
    # packaged separately; the official python images ship it built in.
    if chain.needs_python:
        packages.append("python3-venv")
    text += _apt_layer(doc.get("apt_snapshot", APT_SNAPSHOT), packages, bookworm=bookworm)
    text += "COPY deps/ /opt/deps/manifest/\n"
    if chain.verifier_install:
        text += f"RUN mkdir -p /opt/deps && {chain.verifier_install}\n"
    for step in doc.get("post_install", []):
        text += f"RUN {step}\n"
    # Sorted: the record and the re-loaded toolchain.json must generate the
    # same bytes whatever order their keys were written in.
    for key, value in sorted(doc.get("env", {}).items()):
        text += f"ENV {key}={value}\n"
    # Hidden tests are root-owned and read-only, and the grader runs every test
    # process as the unprivileged `grader` user (grade.py drops privileges), so
    # code a test imports cannot rewrite a later test or its data (reviewer,
    # 2026-09-29: "the delivered verifier does not isolate hidden suite files
    # from imported submission code").
    text += ("COPY . /tests/\n"
             f"RUN mkdir -p /logs/verifier {REPO} {DELIVERABLES} && chmod 755 /tests/test.sh "
             "&& (id -u grader >/dev/null 2>&1 || (echo 'grader:x:2001:2001::/nonexistent:/usr/sbin/nologin' "
             ">> /etc/passwd && echo 'grader:x:2001:' >> /etc/group)) && chmod -R a-w /tests\n"
             "WORKDIR /tests\nCMD [\"sleep\", \"infinity\"]\n")
    return text


def dockerfile_problems(bundle: Path) -> list[str]:
    """Both Dockerfiles must be exactly what the toolchain record generates."""
    bundle = Path(bundle)
    issues = []
    try:
        doc = load(bundle)
    except (OSError, ValueError) as exc:
        return [f"environment/toolchain.json: {exc}"]
    for rel, expected in (("environment/Dockerfile", agent_dockerfile(doc)),
                          ("tests/Dockerfile", verifier_dockerfile(doc))):
        try:
            if (bundle / rel).read_text() != expected:
                issues.append(f"{rel} is not the file environment/toolchain.json generates; re-export")
        except OSError as exc:
            issues.append(f"{rel} is unreadable: {exc}")
    for rel in ("tests", "solution", "authoring", "verification"):
        if f"COPY {rel}" in (bundle / "environment/Dockerfile").read_text():
            issues.append(f"environment/Dockerfile copies {rel}/ into the agent image")
    return issues
