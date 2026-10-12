"""Pipeline configuration: paths, thresholds, and the model tier registry.

Every threshold here was set from a measurement over the corpus, not intuition.
The measurement that produced each one is named in its comment so a future
reader can re-derive it rather than trusting it.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from . import policy as _policy

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def _workspace_setting(name: str) -> str | None:
    """Read one value from workspace.toml beside this package."""
    path = Path(__file__).resolve().parent.parent / "workspace.toml"
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            return (tomllib.load(fh).get("workspace") or {}).get(name)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def dump_root() -> Path:
    """The trace dump this workspace works on.

    Resolution order is env var, then workspace.toml, then a clear failure --
    never a silent default. A module constant pointing at one directory is fine
    while there is one dump and wrong the moment there are two: a pipeline
    quietly indexing last month's corpus produces results that look entirely
    normal.
    """
    configured = os.environ.get("ARENA_DUMP") or _workspace_setting("dump")
    if not configured:
        raise SystemExit(
            "No trace dump configured.\n"
            "  Point this workspace at one:  ./arena dump --set /path/to/dump\n"
            "  or for a single command:      ARENA_DUMP=/path/to/dump ./arena ...")
    root = Path(configured).expanduser()
    if not (root / "sessions").is_dir():
        raise SystemExit(
            f"{root} does not look like a trace dump: no sessions/ directory.")
    return root


def sessions_root() -> Path:
    return dump_root() / "sessions"


def __getattr__(name: str) -> Path:
    """Resolve DUMP_ROOT and SESSIONS on first use (PEP 562).

    Importing this package must not require a configured dump: `arena keys`,
    `arena dump` and `arena status` have to work in a fresh workspace, and a
    module-level constant would fail the import for every command.
    """
    if name == "DUMP_ROOT":
        return dump_root()
    if name == "SESSIONS":
        return sessions_root()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
ARTIFACTS = Path(os.environ.get(
    "ARENA_PIPELINE_ARTIFACTS",
    Path(__file__).resolve().parent.parent / "pipeline-artifacts"))

# --------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------

#: Signals excluded everywhere. `tool_hallucination`: measured 1 failure in
#: 72,398 annotations across this dump (a `pash` typo), and 0 in 12,886 in the
#: previous one. It discriminates nothing.
DEAD_SIGNALS = ("tool_hallucination",)

# --------------------------------------------------------------------------
# Model tiers
# --------------------------------------------------------------------------

#: This dump pools 72 models. A correction on a weak model is weak evidence
#: that a *task* is hard -- the model may simply be incapable of anything. Tier
#: is therefore a confidence weight on trace-derived difficulty evidence, never
#: proof of difficulty: only calibration proves that.
FRONTIER_PREFIXES = (
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-5", "claude-fable-5",
    "gpt-5.6", "gpt-5.5",
    "gemini-3.5", "gemini-3.1-pro",
    "grok-4.3", "grok-4.5", "grok-4.6",
)


def tier(model_name: str | None) -> str:
    m = model_name or ""
    return "frontier" if any(m.startswith(p) for p in FRONTIER_PREFIXES) else "other"


# --------------------------------------------------------------------------
# Scope: coding tasks graded by executed hidden tests
# --------------------------------------------------------------------------

#: This programme builds CODING environments: the agent is dropped into a
#: repository lifted from a real Arena session and must fix, extend or finish
#: it; the verifier executes hidden tests against what it leaves behind. The
#: one truth source is an executed test. A document deliverable is the other
#: workspace's business (`arena-envs-tasks-large-dump`).
#:
#: The funnel is keyed on what a coding fixture is made of -- source files a
#: shipped toolchain can run -- and on the two failure modes the corpus actually
#: carries: a defect the user re-reported after the agent claimed a fix (274
#: persistent_defect moments over 370 coding sessions) and a spec the agent
#: misread (193). See analysis/CODING-TRACES.md.
SCOPE = "coding"

#: Text formats that are recovered alongside source: specs, configs, fixtures
#: and data files a repository carries. They ride along in the fixture; they do
#: not decide scope.
DELIVERABLE_EXT = frozenset({
    ".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".xml",
    ".html", ".rst", ".tex", ".jsonl", ".ini", ".cfg",
})

#: Extensions a source tree is made of. Inventory only: the classifier reads
#: the request and the evidence before scope is decided.
CODE_EXT = frozenset({
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".mts", ".rs",
    ".cs", ".java", ".dart", ".lua", ".luau", ".go", ".c", ".h", ".hpp", ".cpp",
    ".cc", ".vue", ".svelte", ".rb", ".php", ".kt", ".kts", ".swift", ".sh",
    ".bash", ".sql", ".ipynb", ".gradle", ".scala", ".ex", ".exs", ".gd", ".r",
    ".m", ".pl", ".groovy", ".mq5", ".mq4", ".v", ".vhd", ".vhdl", ".css", ".scss",
    ".sass", ".less", ".html", ".htm",
})

#: Session categories the dump labels as coding. Session-level, so they widen
#: the net; the per-seed judgement is the classifier's `coding_request`.
CODING_CATEGORIES = frozenset({
    "code_writing", "code_debugging", "code_explanation", "workflow_automation",
})

#: Truth sources a coding grader can rest on. `executable_tests` is the only
#: one that survives selection; `structural_assertion` (a file exists, a config
#: value is set) is recorded so the accounting can say why a seed fell short.
CODING_TRUTH = ("executable_tests",)

# --------------------------------------------------------------------------
# File classification
# --------------------------------------------------------------------------

TEXT_EXT = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".md", ".txt", ".json", ".html",
    ".css", ".scss", ".rs", ".cs", ".java", ".dart", ".lua", ".luau", ".yml",
    ".yaml", ".toml", ".csv", ".sql", ".sh", ".go", ".c", ".h", ".hpp", ".cpp",
    ".xml", ".vue", ".svelte", ".rb", ".php", ".kt", ".swift", ".r", ".m",
    ".ipynb", ".tf", ".gradle", ".properties", ".ini", ".cfg", ".env",
})

BINARY_EXT = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".pptx", ".docx",
    ".xlsx", ".zip", ".mp3", ".mp4", ".wav", ".jar", ".bin", ".parquet",
    ".so", ".dll", ".exe", ".ttf", ".otf", ".ico", ".woff", ".woff2",
})

MANIFEST_NAMES = frozenset({
    "package.json", "requirements.txt", "Cargo.toml", "pyproject.toml",
    "pom.xml", "build.gradle", "go.mod", "Gemfile", "composer.json",
    "pubspec.yaml", "setup.py", "Makefile", "CMakeLists.txt", "tsconfig.json",
    "Dockerfile", "vite.config.ts", "vite.config.js", "webpack.config.js",
})

#: Tool names whose payloads carry recoverable file bodies.
#: `read_file` and `edit_file` return the WHOLE file in `output.content`;
#: `write_file` carries it in the input. Workspace manifests also carry inline
#: text and references into files/; dump.recover combines all three channels.
BODY_TOOLS_OUTPUT = ("read_file", "edit_file")
BODY_TOOLS_INPUT = ("write_file",)

# --------------------------------------------------------------------------
# Model and reasoning effort
# --------------------------------------------------------------------------

#: The AUTHORING model: the designer and the other non-measurement stages that
#: read it, so an authored result is attributable to a named model rather than
#: to whichever default a module happened to carry. It is NOT the calibration
#: model -- `claude-opus-5` is retired from calibration. Calibration models come
#: from `policy.resolve_calibration_models()`.
ARENA_MODEL = "claude-opus-5"

#: Authoring reasoning effort, sent as `output_config.effort`. The API default
#: is `high`; `xhigh` sits between `high` and `max`.
#:
#: Note what this is NOT: `budget_tokens` is rejected with a 400 on this model,
#: and thinking is adaptive and ON by default -- effort is the only dial, and
#: leaving it unset is a choice of `high`, not a choice of "no thinking".
#:
#: It does NOT apply to calibration. Calibration effort is
#: `policy.SPEC_CALIBRATION_EFFORT` (`high`), reported with every rate; figures
#: taken at a different effort are not comparable.
ARENA_EFFORT = "xhigh"


# --------------------------------------------------------------------------
# Selection thresholds
# --------------------------------------------------------------------------

# HARD FLOORS -- properties we cannot author around. These kill.
#
# Everything else that used to kill is now a TIER BOUNDARY in select/priority.py.
# The distinction matters: drawing one hard line at "2+ corrections and 3+
# documents" left a surviving pool of 6 sessions -- enough for one batch and
# nothing after it -- while ranking a trace with 12 corrections and 27 retained
# files beside one with exactly 2 and 0. Floors decide admissibility; tiers
# decide order.
MIN_TURNS = 2                 # a single-turn seed cannot be multi-step
MIN_CORRECTIONS = 1           # zero pushback is no failure evidence at all
MIN_SOURCE_FILES = 2          # one file is a snippet, not a repository
MIN_SOURCE_BYTES = 2_000      # below this the "repository" is a fragment

#: Categories that cannot carry a deterministic grader on their own. A session
#: is dropped only when its categories are a SUBSET of these -- a session that
#: also writes code is kept, and the aesthetic turns are dropped at seed level.
UNGRADABLE_ONLY = frozenset({"image_video_generation", "chitchat_meta"})

#: Replay-readiness cut. Below it the repository stops being liftable and
#: becomes authored, which is the fidelity complaint we are trying to answer.
REPLAY_READY = 0.55

#: Component weights for the replay-readiness index. They sum to 1.0.
#:
#: Re-keyed for code. What a coding fixture needs is a source tree that is
#: complete at one message boundary (`tree_recoverable`), runs on a shipped
#: toolchain (`runtime`), and carries the changes the corrections were about
#: (`versions`). Vendored material (node_modules, build output) is excluded
#: from every count because it is re-installed from pinned manifests, never
#: lifted. Measured on the 370 coding sessions: 352 have a fully recoverable
#: non-vendored tree at some boundary, median 10 files / 163 KB.
RR_WEIGHTS = {
    "breadth": 0.16,          # recoverable source paths, saturating at 10
    "volume": 0.14,           # source bytes, log-scaled to 200 KB
    "versions": 0.18,         # before/after pairs of the same file
    "tree_recoverable": 0.22, # share of the final tree we hold the bytes of
    "coverage": 0.10,         # recovered paths vs what the hash-trail says changed
    "runtime": 0.12,          # the dominant language runs on a shipped toolchain
    "untruncated": 0.08,      # corpus truncation is 1.27%; penalise the outliers
}

# --------------------------------------------------------------------------
# Calibration bands (PLAYBOOK §3)
# --------------------------------------------------------------------------

FLOOR_BAND = (0.90, 1.01)     # ~100% pass; at most 2; never gates
CORE_BAND = (0.30, 0.70)      # the only band a gate may rest on
STRETCH_BAND = (0.0, 0.30)    # <30%; at most 2; never gates
MIN_CORE_CHECKS = 4
MAX_FLOOR_CHECKS = 2
MAX_STRETCH_CHECKS = 2

# --------------------------------------------------------------------------
# Trap budget
# --------------------------------------------------------------------------

#: Hard floor. A task carrying four or fewer traps is not a hard task; it is a
#: long instruction with a couple of hazards in it. Below this the bundle is
#: refused outright.
MIN_TRAPS = 5

#: Above the floor the expectation SCALES with the trace, because a rich trace
#: can carry more without inventing anything: more documents means more surfaces
#: to hide a conflict on, more usable corrections means more real failure modes,
#: and more scored objectives means more places a trap can bite.
#:
#: Deliberately not a constant. A 3-document seed with 5 corrections and a
#: 20-document seed with 12 are not the same task, and holding both to the same
#: trap count either starves the second or forces the first to invent.
TRAP_SCALE = {"surfaces": 0.5, "corrections": 0.5, "objectives": 0.25}


def expected_traps(surfaces: int, usable_corrections: int, objectives: int) -> int:
    """Traps a bundle of this size should carry. Never below MIN_TRAPS."""
    scaled = (TRAP_SCALE["surfaces"] * surfaces
              + TRAP_SCALE["corrections"] * usable_corrections
              + TRAP_SCALE["objectives"] * objectives)
    return max(MIN_TRAPS, round(scaled))


# --------------------------------------------------------------------------
# Concurrency and rate limits: at the maximum by default
# --------------------------------------------------------------------------
# Operator decision, 2026-09-24: run at maximum. The binding capacity is the
# one Coder calibration workspace -- 20 concurrent campaigns (the QC
# template's rated 20-concurrency; operator decision 2026-09-25: one workspace,
# never a pool of several). Everything upstream is sized to keep it full; the
# provider accounts' own RPM/TPM limits remain the real ceiling, and 429s are
# retried by the transport rather than counted as results.

#: Concurrent native campaigns the workspace can hold (`coder.Settings`).
POOL_CAMPAIGNS = 20
#: Tasks screened or qualifying at once. Each runs its two model campaigns in
#: parallel.
MAX_QUALIFY_WORKERS = POOL_CAMPAIGNS // 2
#: Tasks authoring at once, sized to keep qualification fed.
MAX_TASK_WORKERS = MAX_QUALIFY_WORKERS
#: Batch-wide rollout slots for authoring-time model calls, one per pool campaign.
MAX_ROLLOUT_BUDGET = POOL_CAMPAIGNS
#: In-flight provider requests for authoring, review and classification.
MAX_PROVIDER_CONCURRENCY = POOL_CAMPAIGNS
#: Semantic reviews running at once. Each is a long-context agent rollout on
#: the reviewer model, and a resumed 40-task batch sent 13 at once and lost
#: every one to the provider's rate limit (build 2026-09-26). Reviews queue;
#: authoring, on the other provider, is not affected.
MAX_REVIEW_CONCURRENCY = 6

#: Turns the classifier's evidence loop may take before it gives up on a seed.
#: The first --need 60 run (2026-09-28) lost 7 of 105 seeds at 32 turns to
#: "budget exhausted" -- three of them from one rich session (019ed766) -- and a
#: budget loss is not a verdict. With the prefix served from cache the extra
#: turns cost little; a seed that still cannot be read in this many turns is
#: recorded as an error and re-run, never counted as rejected.
CLASSIFIER_MAX_TURNS = 64

#: Pre-reviews a design session may run: the independent reviewer over the
#: staged draft, exactly as the gate runs it. Each costs one review; the
#: designer is told to use one once its controls are clean and another after
#: addressing the findings. Advisory to the designer; the gate's own review
#: still decides.
#: 3 until 2026-09-29 01:35: the first designer to use all three moved 16 -> 25 -> 14
#: findings, converging only on the third pass; a twenty-check task needs more.
MAX_PRE_REVIEWS_PER_SESSION = 5

#: Independent solves a design session may run: a different vendor's model fixes
#: the task from the instruction and the repository alone and the shipped
#: grader scores its diff. Advisory to the designer; each costs one agentic
#: session plus a grading run.
#: 2 until 2026-09-29 04:15: every first draft measured 1.0 on its first solve and
#: the designers then hardened; a second solve reads the hardening and a third is
#: the only way to learn whether a second hardening pass moved it. Each costs one
#: agentic session (6-10 min) -- a fiftieth of the 5x2 screen it anticipates.
MAX_BLIND_SOLVES_PER_SESSION = 3
#: Hard stop on provider requests per process: a runaway-spend guard, not a
#: throttle. Exhausting it stops the run loudly (`ProviderBudgetExhausted`).
MAX_API_REQUESTS = 20_000


#: Batch discipline: author, validate, calibrate, review, re-fit -- then the
#: next batch. Small enough that a systemic defect costs one batch, not ten.
BATCH_SIZE = 3
ROLLOUTS_PER_STEP = 10


#: Bundle directory names. `c-001-<task-name>` is the working-tree scheme;
#: `a_f2_<task-name>` is the delivered name inside `F2-scenario/` etc. Both are
#: matched -- a tool that silently skipped one tree would report a clean sweep
#: over half the corpus.
BUNDLE_GLOBS = ("c-[0-9]*", "a_f[0-3]_*")


#: Bundles are grouped by category on disk, so a reader can see what kind of
#: task they are looking at without opening a ledger. The directory name is the
#: category's own word, not its letter -- `rich/` reads; `A/` does not.
#: The names are `policy.TARGET_LEVEL_DIRS`; this is an alias, never a copy.
#: Trees built before 2026-09-26 use `policy.LEGACY_CATEGORY_DIRS`
#: (rich/strong/fixture-led/thin); `orchestrate._bundle_for` still finds them.
CATEGORY_DIRS = _policy.TARGET_LEVEL_DIRS


def bundle_paths(root):
    """Every bundle under `root`, at the top level or inside a category folder.

    Both, because the two layouts coexist: everything built before the grouping
    sits flat, and a sweep that found only one of them would report a clean
    result over half the corpus.
    """
    from pathlib import Path
    root = Path(root)
    found = set()
    for pattern in BUNDLE_GLOBS:
        found.update(p for p in root.glob(pattern) if p.is_dir())
        found.update(p for p in root.glob(f"*/{pattern}") if p.is_dir())
    return sorted(found)


# ---------------------------------------------------------------------------
# Coding tasks
# ---------------------------------------------------------------------------

#: Extension -> language, for fixture inventories, index rows and runtime
#: feasibility. Engine-bound languages (Roblox Lua, Unity C#, Flutter Dart,
#: Godot GDScript, MQL5) are mapped too; whether they can run is decided by
#: `runtime_class`, not here.
LANGUAGE_BY_EXT = {
    ".py": "python", ".pyi": "python", ".ipynb": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript",
    ".vue": "javascript", ".svelte": "javascript",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "css", ".sass": "css", ".less": "css",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala",
    ".rb": "ruby", ".php": "php", ".cs": "csharp", ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp",
    ".hpp": "cpp", ".swift": "swift", ".m": "objc", ".lua": "lua", ".luau": "lua", ".sh": "shell",
    ".bash": "shell", ".zsh": "shell", ".sql": "sql", ".dart": "dart", ".gd": "gdscript", ".r": "r",
    ".ex": "elixir", ".exs": "elixir", ".pl": "perl", ".groovy": "groovy", ".mq5": "mql5", ".mq4": "mql5",
    ".v": "hdl", ".vhd": "hdl", ".vhdl": "hdl",
}

#: Languages a shipped toolchain runs natively (author/toolchain.TOOLCHAINS).
NATIVE_LANGUAGES = {"python", "javascript", "typescript", "go", "rust", "php", "c", "cpp", "shell", "sql"}

#: Languages graded through a headless browser in the verifier.
BROWSER_LANGUAGES = {"html", "css"}

#: Languages bound to an engine or platform no container here provides.
ENGINE_LANGUAGES = {"lua", "csharp", "dart", "gdscript", "mql5", "hdl", "kotlin", "swift", "objc", "java"}

#: What a seed's dominant language can run on:
#:   native   a shipped toolchain (python, node, go, rust, php, c, shell)
#:   browser  html/css/js graded through headless Chromium in the verifier
#:   engine   bound to a platform no container here provides (Roblox, Unity,
#:            Flutter, Godot, MQL5, HDL, JVM/Swift until a toolchain ships)
#:   none     no source at all
RUNTIME_CLASSES = ("native", "browser", "engine", "none")

#: Runtime classes a task can be built on today.
BUILDABLE_RUNTIMES = ("native", "browser")

#: Path components that mark vendored or generated material. Never lifted into
#: a fixture (re-installed from pinned manifests) and excluded from every
#: recoverability count, or a 40 MB node_modules would make a session look rich.
VENDORED_PARTS = frozenset({
    "node_modules", ".next", "dist", "build", ".git", "venv", ".venv",
    "__pycache__", ".cache", ".npm", ".local", ".config", "site-packages",
    ".pytest_cache", ".mypy_cache", "target", ".gradle", ".m2", "coverage",
    ".nyc_output", ".parcel-cache", ".turbo",
})

#: Test-runner invocations recognised in the trace's shell calls. A session
#: that ran one had an executable oracle of its own; only 17 of 370 did.
TEST_RUNNER_PATTERNS = (
    r"\bpytest\b", r"\bpython3?\s+-m\s+(?:pytest|unittest)\b", r"\bunittest\b",
    r"\bnpm\s+(?:run\s+)?test\b", r"\bpnpm\s+(?:run\s+)?test\b", r"\byarn\s+test\b",
    r"\bnpx\s+(?:jest|vitest|mocha|playwright)\b", r"\b(?:jest|vitest|mocha)\b",
    r"\bgo\s+test\b", r"\bcargo\s+test\b", r"\bphpunit\b", r"\bgradle\w*\s+test\b",
    r"\bmvn\s+test\b", r"\bmake\s+test\b",
)


def language_of(path: str) -> str | None:
    """Programming language of a path by extension, or None for non-source."""
    from pathlib import PurePosixPath
    return LANGUAGE_BY_EXT.get(PurePosixPath(path or "").suffix.lower())


def runtime_class(bytes_by_language: dict) -> str:
    """Runtime class of a source tree from its byte share per language.

    The dominant language decides: a Python service with HTML templates is
    native, a static site with a little JS is browser, a Unity project with a
    Python helper script is engine. Markup with no script at all is still
    browser -- a page can be graded in Chromium without any JS.
    """
    if not bytes_by_language:
        return "none"
    dominant = max(bytes_by_language, key=lambda k: (bytes_by_language[k], k))
    if dominant in ENGINE_LANGUAGES:
        return "engine"
    if dominant in BROWSER_LANGUAGES:
        return "browser"
    if dominant in NATIVE_LANGUAGES:
        return "native"
    return "engine"
