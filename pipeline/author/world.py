"""The environment is a world: generated, realistic data and state around the trace's real code.

Arena, 2026-10-30 (via the user): make the environments complex -- vast, realistic, synthetic data
that looks real -- so a model needs time to figure things out along its natural trajectory, while
the tasks stay extremely hard with many diverse traps. Measured on our own calibrations the same
day: 6,241 coding episodes over 196 tasks had a median agent time of 6.8 min (Fable) and 5.6 min
(Astra), 15,680 non-coding episodes 2.4 and 1.3 min, and failed episodes were no longer than
passed ones -- the models are quickly and confidently wrong because there is little to discover.
The largest quarter of fixtures (median 81 files) ran barely longer than the smallest (6.5 files).

So size alone is not the lever (doctrine: breadth and volume measured dead; Terminal-Bench's task
guidelines; GSM-NoOp's audited replication). What the research says does bite over realistic data
(CoDA-Bench, Era by Eon, DABstep; analysis in the 2026-10-30 session): a defect that shows on a
small ordinary-looking part of a realistic population, distractors that are MEMBERS of that
population, facts spread over code, data and docs that the agent must find by running things,
and multi-hop / temporal / ranking decisions rather than simple filters.

This module gives the designer:
  * `PROMPT`             the doctrine (in the designer's system prompt);
  * `run_generator`      executes a seeded generator in the pinned python image, twice, and
                         requires identical bytes (no network, no clock, no hash-order luck);
  * `profile`            a realism scorecard and synthetic-artifact detector over the outputs
                         (numbered names, flat histograms, counter timestamps, identical child
                         counts, repeated filler, placeholders, round numbers);
  * `requirement_gaps`   the creation components a fresh design or a hardening round still owes
                         (advisory: the author is strengthened, never blocked);
  * `synthetic_files`    the generated files, labelled for the package (Arena spec: "Label
                         invented data as synthetic").
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import shutil
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from .. import policy
from ..artifacts import atomic_json

SCHEMA = "arena-coding/world/1"
RECORD = "verification/world.json"
GENERATORS = "authoring/world"
LIB = Path(__file__).parent / "world_lib" / "arena_world.py"
GENERATOR_TIMEOUT_S = 300


class WorldError(RuntimeError):
    pass


PROMPT = """\
THE ENVIRONMENT IS A WORLD, NOT A TOY (Arena feedback, user 2026-10-30). Arena asks for complex, \
realistic environments a model needs time to understand. Measured on our own calibrations: the median \
episode took 6.8 min (Fable) and 5.6 min (Astra), failed episodes were no longer than passed ones -- the \
models are quickly and confidently wrong because there is nothing to discover -- and bigger fixtures barely \
changed that. Build the world the program lives in, at authoring time:
  - REAL CODE, SYNTHETIC WORLD. The code stays lifted from the trace. Around it, build the data and state \
the program runs on -- records, exports, logs, configs, queues, caches, earlier runs' output, the user's \
saved state -- at the scale and messiness the user's real system would have. Ground it in the trace: the \
schema, columns, formats, units, identifiers, time zones and producers the user's files and words show. \
Never another domain.
  - GENERATE IT, NEVER TYPE IT. generate_world runs your seeded generator (stdlib plus the arena_world \
helpers it documents) twice in a pinned python image and writes the outputs into the fixture (declared \
synthetic, labelled in the package) or into suites/data (hidden draws from other seeds). Hand-typed tables \
carry the tells its profile flags. Fix every artifact the profile reports and re-run.
  - REALISTIC: heavy-tailed volumes (a few parents own most children), skewed amounts with real price \
endings, correlated fields, business-day timestamps in a real UTC offset with irregular gaps, genuine \
same-name pairs, diacritics and apostrophes, identifiers with gaps. Never User1/User2, flat histograms, \
counters that advance evenly, identical child counts, repeated filler, lorem/foo/John Doe/Acme.
  - WHAT MAKES A WORLD BITE (measured elsewhere, a hypothesis here until the screen measures it):
      (a) the defect shows only on a SMALL, ordinary-looking part of the population: long-tail members \
(Fable's miss), members in another shape or identity (Astra's miss), NEAR MISSES that each break exactly \
ONE condition of a published rule, rotated over the conditions (arena_world.rotate_violations) -- each one \
a lever of an interlock on a hidden item, proved by its mutant;
      (b) graded decisions are the shapes that collapse frontier accuracy on realistic data: multi-hop \
joins across files or tables, superlatives and rankings with ties, temporal aggregation (period edges, \
offsets, DST, late or retracted records), exhaustive aggregation over duplicates -- simple filtering is \
what models get right (Era by Eon: filtering 93%, multi-hop 4%, temporal 7%, ranking 11%);
      (c) distractors are MEMBERS of the population -- last year's export, the other region's file, a \
superseded config, a retracted record -- in the same schema and topic. Random noise files are filtered \
in seconds and are dead. Never label a decoy, never warn about it;
      (d) DISCOVERY: the facts a correct fix needs are spread over code, data, docs and logs and are \
found by running the program on the world, never stated in one hub file or pointed at;
      (e) the program must be RUN to see the problem: the visible symptom is the program's real output \
on the world, and the cause is reached by observing, not by reading one function;
      (f) DEPTH: fixing the reported symptom exposes the next graded defect on the same world, each its \
own decision with its own mutant -- per-step reliability compounds.
  - LONG BY DESIGN, NEVER BY PADDING: no sleeps, slow builds, resource caps, time warnings, huge irrelevant \
files, gigabytes of data or more cases of one rule. A long episode is one where the agent explores, runs, \
observes and revises. Big enough that reading everything does not settle the task and the population \
carries its long tail: typically thousands of records over several files.
  - VALIDITY IS UNCHANGED: every graded rule is a sentence of the instruction; expected values are derived \
by running code on the hidden draws, never stored; hidden data never appears in the repository; the \
reference and the alternative pass every draw; the reference grades identically on every run.
  - RECORD IT in quality.world {generators: [names], population_traps: [{trap, members, shape}], \
discovery_path, why_long}; creation_status lists what is missing. A task with genuinely no data or state \
to build (a pure algorithm on its arguments) records quality.world.declined with the reason instead."""


TOOL_GENERATE = {
    "name": "generate_world",
    "description": (
        "Run a seeded data/state generator (Python 3.12 stdlib + `import arena_world`; call with "
        "describe_library=true first to read the helpers) TWICE in a pinned python image with no network; "
        "both runs must write identical bytes under ARENA_WORLD_OUT. target='fixture' writes the outputs under "
        "environment/repo/<out_dir> (declared synthetic, zero fidelity credit, labelled in the package); "
        "target='suites' stages them as hidden inputs under suites/data/<out_dir> (draw them from a DIFFERENT "
        "seed than the fixture), and ship_generator=true also stages the generator and arena_world under "
        "suites/world/<name>/ so hidden tests can draw at test time. Returns the files, records and the "
        "realism profile (artifacts to fix). Re-running a name replaces that generator's previous outputs."),
    "input_schema": {"type": "object", "properties": {
        "name": {"type": "string", "description": "generator name, [a-z0-9_-]"},
        "generator": {"type": "string", "description": "the Python source"},
        "seed": {"type": "integer"},
        "target": {"type": "string", "enum": ["fixture", "suites"]},
        "out_dir": {"type": "string", "description": "relative directory the outputs land under ('' = root)"},
        "grounded_in": {"type": "array", "items": {"type": "string"},
                        "description": "what in the trace the schema and formats come from (session::msgN::path or quotes)"},
        "purpose": {"type": "string"},
        "ship_generator": {"type": "boolean"},
        "describe_library": {"type": "boolean", "description": "return the arena_world helper documentation only"}},
        "required": ["name"]},
}

TOOL_PROFILE = {
    "name": "world_profile",
    "description": ("The realism scorecard and synthetic-artifact detector over fixture files (default: every "
                    "generated file; or the paths given). Advisory."),
    "input_schema": {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}}},
}


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------

def _path(bundle: Path) -> Path:
    return Path(bundle) / RECORD


def load(bundle: Path) -> dict | None:
    try:
        doc = json.loads(_path(bundle).read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == SCHEMA else None


def arm(bundle: Path, required: bool, reason: str) -> dict:
    """Open the session's world requirement (fresh designs and hardening rounds; never repairs)."""
    doc = load(bundle) or {"schema": SCHEMA, "runs": []}
    doc["required"] = bool(required)
    doc["armed_reason"] = reason
    doc["armed_at"] = time.time()
    atomic_json(_path(bundle), doc)
    return doc


def record_run(bundle: Path, run: dict) -> None:
    doc = load(bundle) or {"schema": SCHEMA, "runs": [], "required": False}
    doc["runs"] = [r for r in doc.get("runs") or [] if not (r.get("name") == run["name"] and r.get("target") == run["target"])]
    doc["runs"].append(run)
    atomic_json(_path(bundle), doc)


def library_text() -> str:
    text = LIB.read_text(encoding="utf-8")
    doc = text.split('"""', 2)[1] if text.startswith('"""') else ""
    signatures = [line.strip() for line in text.splitlines() if line.startswith("def ") or line.startswith("class ")
                  or line.startswith("    def ")]
    return doc.strip() + "\n\nAPI:\n" + "\n".join(signatures)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _limits(files: dict[str, bytes]) -> None:
    if len(files) > policy.WORLD_MAX_FILES:
        raise WorldError(f"the generator wrote {len(files)} files; the limit is {policy.WORLD_MAX_FILES}")
    total = sum(len(b) for b in files.values())
    if total > policy.WORLD_MAX_OUTPUT_BYTES:
        raise WorldError(f"the generator wrote {total} bytes; the limit is {policy.WORLD_MAX_OUTPUT_BYTES} "
                         "(scale is discovery and a long tail, not volume)")
    for rel, data in files.items():
        if len(data) > policy.WORLD_MAX_FILE_BYTES:
            raise WorldError(f"{rel} is {len(data)} bytes; one file may hold {policy.WORLD_MAX_FILE_BYTES}")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise WorldError(f"{rel} is not UTF-8 text; binaries never enter a coding fixture") from None


def _read_tree(root: Path) -> dict[str, bytes]:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise WorldError(f"{path.relative_to(root).as_posix()} is a symlink; write regular files only")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def run_generator(source: str, seed: int, *, timeout_s: int = GENERATOR_TIMEOUT_S) -> dict:
    """Run `source` twice in the pinned python image; the outputs must be byte-identical."""
    from . import sandbox, toolchain
    ok, why = sandbox.available()
    if not ok:
        raise WorldError(f"no sandbox: {why}")
    started = time.monotonic()
    runs: list[dict[str, bytes]] = []
    logs = []
    with tempfile.TemporaryDirectory(prefix="arena-world-") as tmp, \
            sandbox.Container(toolchain.IMAGES["python"], memory="4g", cpus="2") as box:
        box.exec("mkdir -p /world /out1 /out2")
        box.put_bytes(LIB.read_bytes(), "/world/arena_world.py")
        box.put_bytes(source.encode("utf-8"), "/world/generate.py")
        for n, out_dir in enumerate(("/out1", "/out2"), 1):
            result = box.exec("cd /world && python3 -s generate.py", timeout=timeout_s,
                              env={"ARENA_WORLD_SEED": str(int(seed)), "ARENA_WORLD_OUT": out_dir})
            logs.append(result)
            if result["timed_out"] or result["exit_code"]:
                raise WorldError(f"the generator {'timed out' if result['timed_out'] else 'failed'} (run {n}):\n"
                                 + (result["stderr"] or result["stdout"])[-4000:])
            dest = Path(tmp) / f"run{n}"
            box.capture(out_dir, ["."], [], dest)
            runs.append(_read_tree(dest))
    first, second = runs
    if not first:
        raise WorldError("the generator wrote no files under ARENA_WORLD_OUT")
    differing = sorted(rel for rel in set(first) | set(second) if first.get(rel) != second.get(rel))
    if differing:
        raise WorldError("the two runs differ (draw every value from World(...).stream(name); never iterate a "
                         f"set, read the clock or the environment): {differing[:5]}")
    _limits(first)
    return {"files": first, "seconds": round(time.monotonic() - started, 1),
            "stdout": logs[0]["stdout"][-2000:], "stderr": logs[0]["stderr"][-2000:]}


# ---------------------------------------------------------------------------
# Realism profile
# ---------------------------------------------------------------------------

_MISSING = {"", "na", "n/a", "-", "null", "none", "?", "tbd", "nan"}
_ID_NAME = re.compile(r"(^|_|-)(id|uuid|guid|key|code|ref|no|num|number|sku)$|[a-z]Id$", re.I)
_NUMBERED = re.compile(r"^([A-Za-z][A-Za-z .'-]*?)[ _-]?(\d{1,4})$")
_PLACEHOLDER = re.compile(r"\b(lorem|ipsum|dolor sit|foo|bar|baz|qux|dummy|placeholder|john doe|jane doe|acme|"
                          r"test ?user|sample ?\d+|example ?\d+|product ?\d+|customer ?\d+|user ?\d+|item ?\d+)\b", re.I)
_LOG_TS = re.compile(r"^\[?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)")
_DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M:%S",
               "%m/%d/%Y %H:%M", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d %b %Y", "%Y/%m/%d", "%d.%m.%Y")
MAX_PROFILE_ROWS = 50_000


def _parse_dt(value: str) -> datetime | None:
    text = value.strip()
    if not text or len(text) > 40:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _number(value: str) -> float | None:
    text = value.strip().replace(",", "").replace("$", "").replace(" ", "")
    if not text or len(text) > 30:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _tables(rel: str, data: bytes) -> list[tuple[str, list[str], list[list[str]]]]:
    """(name, columns, rows) for the tabular content of one file; a log yields its timestamps."""
    text = data.decode("utf-8", errors="replace").lstrip("﻿")
    suffix = Path(rel).suffix.lower()
    if suffix in (".csv", ".tsv", ".psv"):
        head = text.split("\n", 1)[0]
        delimiter = max(",;\t|", key=head.count)
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = []
        for i, row in enumerate(reader):
            rows.append(row)
            if i > MAX_PROFILE_ROWS:
                break
        if len(rows) < 2:
            return []
        return [(rel, rows[0], rows[1:])]
    if suffix in (".jsonl", ".ndjson"):
        objects = []
        for line in text.splitlines()[:MAX_PROFILE_ROWS]:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                objects.append(value)
        return [_object_table(rel, objects)] if objects else []
    if suffix == ".json":
        try:
            value = json.loads(text)
        except ValueError:
            return []
        lists = []

        def walk(node, name, depth):
            if isinstance(node, list) and node and all(isinstance(x, dict) for x in node[:50]):
                lists.append((name, node))
            if depth < 2 and isinstance(node, dict):
                for key, child in node.items():
                    walk(child, f"{name}:{key}", depth + 1)
        walk(value, rel, 0)
        return [_object_table(name, rows[:MAX_PROFILE_ROWS]) for name, rows in lists]
    stamps = []
    for line in text.splitlines()[:MAX_PROFILE_ROWS]:
        match = _LOG_TS.match(line)
        if match:
            stamps.append([match.group(1)])
    return [(rel, ["timestamp"], stamps)] if len(stamps) >= 20 else []


def _object_table(name: str, objects: list[dict]):
    columns = []
    for obj in objects:
        for key, value in obj.items():
            if key not in columns and not isinstance(value, (dict, list)):
                columns.append(key)
    rows = [["" if obj.get(c) is None else str(obj.get(c)) for c in columns] for obj in objects]
    return name, columns, rows


def _moments(values: list[float]) -> tuple[float, float]:
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    if var == 0:
        return 0.0, 0.0
    sd = math.sqrt(var)
    skew = sum(((v - mean) / sd) ** 3 for v in values) / n
    kurt = sum(((v - mean) / sd) ** 4 for v in values) / n - 3
    return skew, kurt


def _column_findings(table: str, column: str, values: list[str]) -> tuple[list[dict], dict]:
    present = [v.strip() for v in values if v.strip().lower() not in _MISSING]
    findings, notes = [], {}
    if len(present) < 20:
        return findings, notes

    def flag(axis, kind, detail):
        findings.append({"file": table, "column": column, "axis": axis, "kind": kind, "detail": detail})

    numbers = [x for x in (_number(v) for v in present) if x is not None]
    stamps = [x for x in (_parse_dt(v) for v in present) if x is not None] if len(numbers) < 0.9 * len(present) else []
    id_like = bool(_ID_NAME.search(column))
    counts = Counter(present)
    if id_like:
        per_parent = Counter(counts.values())
        if len(counts) >= 10 and len(present) / len(counts) >= 2:
            top = per_parent.most_common(1)[0]
            if top[1] / len(counts) >= 0.8:
                flag("structural", "identical_child_counts",
                     f"{top[1]} of {len(counts)} parents have exactly {top[0]} rows (real volumes are heavy-tailed)")
            ranked = sorted(counts.values(), reverse=True)
            head = max(1, len(ranked) // 10)
            notes["top_decile_share"] = round(sum(ranked[:head]) / len(present), 2)
        return findings, notes
    if len(numbers) >= 0.9 * len(present):
        if len(numbers) >= 100 and len(set(numbers)) >= 30:
            skew, kurt = _moments(numbers)
            notes.update(skew=round(skew, 2), excess_kurtosis=round(kurt, 2))
            if abs(skew) < 0.2 and -1.4 < kurt < -0.95:
                flag("marginal", "uniform_numeric",
                     f"values look uniformly drawn (skew {skew:.2f}, excess kurtosis {kurt:.2f}); real amounts and "
                     "counts are skewed with a long tail")
        large = [x for x in numbers if abs(x) >= 10]
        if len(large) >= 50:
            round_share = sum(1 for x in large if x == int(x) and int(x) % 10 == 0) / len(large)
            if round_share >= 0.6:
                flag("marginal", "round_numbers", f"{round_share:.0%} of values are multiples of 10")
        return findings, notes
    if len(stamps) >= 0.8 * len(present):
        ordered = sorted(s.replace(tzinfo=None) for s in stamps)
        diffs = [(b - a).total_seconds() for a, b in zip(ordered, ordered[1:]) if b > a]
        if len(diffs) >= 19:
            step, hits = Counter(diffs).most_common(1)[0]
            if hits / len(diffs) >= 0.6:
                flag("temporal", "counter_timestamps",
                     f"{hits / len(diffs):.0%} of consecutive events are exactly {step:g}s apart")
        times = Counter(s.strftime("%H:%M:%S") for s in stamps)
        if not (len(times) == 1 and "00:00:00" in times):
            clock, hits = times.most_common(1)[0]
            if hits / len(stamps) >= 0.6:
                flag("temporal", "fixed_time_of_day", f"{hits / len(stamps):.0%} of events happen at {clock}")
        weekend = sum(1 for s in stamps if s.weekday() >= 5) / len(stamps)
        notes["weekend_share"] = round(weekend, 2)
        return findings, notes
    numbered = Counter()
    stems = {}
    for value in counts:
        match = _NUMBERED.match(value)
        if match:
            stems.setdefault(match.group(1).strip().lower(), set()).add(match.group(2))
    series = {stem for stem, nums in stems.items() if len(nums) >= 3}
    for value, count in counts.items():
        match = _NUMBERED.match(value)
        if match and match.group(1).strip().lower() in series:
            numbered[value] = count
    if len(counts) >= 20 and sum(numbered.values()) / len(present) >= 0.2:
        example = ", ".join(list(numbered)[:3])
        flag("content", "numbered_names", f"{sum(numbered.values()) / len(present):.0%} of values are numbered "
                                          f"variants of one stem ({example})")
    placeholder = sum(c for v, c in counts.items() if _PLACEHOLDER.search(v))
    if placeholder / len(present) >= 0.05:
        flag("content", "placeholder_text", f"{placeholder / len(present):.0%} of values are placeholder text")
    mean_len = sum(len(v) for v in present) / len(present)
    if mean_len >= 25:
        value, hits = counts.most_common(1)[0]
        if hits / len(present) >= 0.3 or len(counts) / len(present) < 0.1:
            flag("content", "repeated_filler", f"the same long text repeats ({hits} of {len(present)} rows: "
                                               f"{value[:60]!r})")
    elif 3 <= len(counts) <= 12 and len(present) >= 100:
        ranked = counts.most_common()
        if ranked[0][1] / ranked[-1][1] <= 1.25:
            flag("marginal", "flat_categories", f"all {len(counts)} categories are about equally common "
                                                f"({ranked[0][1]}..{ranked[-1][1]}); real categories are skewed")
    return findings, notes


def profile(files: dict[str, bytes]) -> dict:
    """Realism over generated (or any) text files: per-axis scores and every artifact found."""
    artifacts, measured_axes, tables_out, records = [], set(), {}, 0
    for rel, data in sorted(files.items()):
        for name, columns, rows in _tables(rel, data):
            records += len(rows)
            notes = {}
            for index, column in enumerate(columns):
                values = [row[index] if index < len(row) else "" for row in rows]
                found, column_notes = _column_findings(name, str(column), values)
                artifacts += found
                if column_notes:
                    notes[str(column)] = column_notes
                if sum(1 for v in values if v.strip()) >= 20:
                    measured_axes.update({"content", "marginal"})
                    if column_notes.get("weekend_share") is not None:
                        measured_axes.add("temporal")
                    if column_notes.get("top_decile_share") is not None:
                        measured_axes.add("structural")
            tables_out[name] = {"rows": len(rows), "columns": len(columns), "notes": notes}
    scores = {}
    for axis in ("marginal", "temporal", "structural", "content"):
        if axis in measured_axes or any(a["axis"] == axis for a in artifacts):
            scores[axis] = max(0, 100 - 25 * sum(1 for a in artifacts if a["axis"] == axis))
    realism = round(sum(scores.values()) / len(scores)) if scores else None
    return {"realism": realism, "scores": scores, "artifacts": artifacts, "records": records,
            "tables": tables_out, "not_measured": sorted({"marginal", "temporal", "structural", "content"} - set(scores))}


# ---------------------------------------------------------------------------
# Creation requirement, package labels
# ---------------------------------------------------------------------------

def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def live_runs(bundle: Path) -> list[dict]:
    """Fixture runs whose outputs are still in the fixture, byte for byte."""
    doc = load(bundle) or {}
    repo = Path(bundle) / "environment/repo"
    live = []
    for run in doc.get("runs") or []:
        if run.get("target") != "fixture":
            continue
        outputs = run.get("outputs") or {}
        if outputs and all((repo / rel).is_file() and hashlib.sha256((repo / rel).read_bytes()).hexdigest() == sha
                           for rel, sha in outputs.items()):
            live.append(run)
    return live


def requirement_gaps(spec: dict, bundle: Path) -> list[str]:
    doc = load(bundle)
    if not doc or not doc.get("required"):
        return []
    quality = spec.get("quality") if isinstance(spec.get("quality"), dict) else {}
    world = quality.get("world") if isinstance(quality.get("world"), dict) else {}
    declined = world.get("declined")
    if _text(declined) and len(declined.strip()) >= policy.WORLD_DECLINE_REASON_MIN:
        return []
    gaps = []
    live = live_runs(bundle)
    if not live:
        gaps.append("ENVIRONMENT WORLD (0 generated): no generate_world output is in the fixture -- build the data "
                    "and state the program runs on (THE ENVIRONMENT IS A WORLD), or record quality.world.declined "
                    f"with the reason this task has none ({policy.WORLD_DECLINE_REASON_MIN}+ characters)")
    else:
        records = sum(int(run.get("records") or 0) for run in live)
        if records < policy.WORLD_MIN_RECORDS:
            gaps.append(f"ENVIRONMENT WORLD scale ({records}/{policy.WORLD_MIN_RECORDS} records): the population "
                        "must carry its long tail and near misses; reading it all must not settle the task")
        flagged = [a for run in live for a in (run.get("profile") or {}).get("artifacts") or []]
        for artifact in flagged[:5]:
            gaps.append(f"ENVIRONMENT WORLD realism: {artifact['file']} [{artifact['column']}] {artifact['kind']} -- "
                        f"{artifact['detail']}; fix the generator and re-run generate_world")
    for key in ("discovery_path", "why_long"):
        if not _text(world.get(key)):
            gaps.append(f"quality.world.{key}")
    if not (isinstance(world.get("generators"), list) and world["generators"]):
        gaps.append("quality.world.generators")
    trap_ids = {t.get("id") for t in spec.get("traps") or [] if isinstance(t, dict)}
    population = [row for row in world.get("population_traps") or []
                  if isinstance(row, dict) and row.get("trap") in trap_ids and _text(row.get("members"))
                  and _text(row.get("shape"))]
    if len(population) < policy.MIN_POPULATION_TRAPS:
        gaps.append(f"quality.world.population_traps ({len(population)}/{policy.MIN_POPULATION_TRAPS}): traps whose "
                    "hidden item is a small ordinary-looking part of the world -- long-tail members, another shape "
                    "or identity, near misses breaking exactly one condition -- each {trap, members, shape}")
    return gaps


def synthetic_files(bundle: Path) -> list[dict]:
    """Every generated fixture file still in the fixture, with its generator and seed (the package label)."""
    rows = []
    for run in live_runs(bundle):
        for rel, sha in sorted((run.get("outputs") or {}).items()):
            rows.append({"path": rel, "sha256": sha, "generator": run.get("generator"), "seed": run.get("seed"),
                         "label": "synthetic"})
    return rows


def save_generator(bundle: Path, name: str, source: str) -> str:
    rel = f"{GENERATORS}/{name}/generate.py"
    target = Path(bundle) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    shutil.copyfile(LIB, target.parent / "arena_world.py")
    return rel
