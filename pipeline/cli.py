"""`arena` -- one entry point for the whole pipeline.

Subcommands mirror the stages, and every one of them can be run on its own
because the stages hand artefacts to each other through files rather than
memory. `status` reads those artefacts, so it reports what actually happened
rather than what a run intended.

Credentials come from `keys.env` in the repository root. If it is absent the
first command that needs it asks, once, and writes the file with owner-only
permissions. Keys are never echoed back, never logged, and never printed by
`status`, which shows only whether each one is set.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from . import config as _config
from . import policy

ROOT = Path(__file__).resolve().parent.parent
KEYS = ROOT / "keys.env"
ARTIFACTS = ROOT / "pipeline-artifacts"
WORKSPACE = ROOT / "workspace.toml"

REQUIRED_KEYS = {
    "ANTHROPIC_AUTH_TOKEN": "Anthropic OAuth token (classification, design, calibration)",
    "ANTHROPIC_API_KEY": "Anthropic API key -- alternative to the OAuth token above",
    "OPENAI_API_KEY": "OpenAI API key (optional: cross-model calibration)",
}
# One Anthropic credential, either shape. Every call site already reads
# `ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY` -- routing an `sk-ant-oat*` token
# to `Authorization: Bearer` and a key to `x-api-key` -- but this gate demanded
# the key specifically, so a workspace holding only the OAuth token was refused
# a credential it could actually use.
ESSENTIAL_GROUPS = (("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"),)
ESSENTIAL = tuple(name for group in ESSENTIAL_GROUPS for name in group)


def _unsatisfied(values: dict[str, str]) -> list[str]:
    """Groups with no credential set, rendered as 'A or B'."""
    return [" or ".join(group) for group in ESSENTIAL_GROUPS
            if not any(values.get(name) for name in group)]


def load_keys(prompt_if_missing: bool = True) -> dict[str, str]:
    """Read keys.env into the environment, creating it if it is absent."""
    from . import openai_transport
    values = openai_transport.read_key_file(KEYS)

    missing = _unsatisfied(values)
    if missing and prompt_if_missing:
        if not sys.stdin.isatty():
            raise SystemExit(
                f"keys.env is missing {', '.join(missing)} and there is no "
                "terminal to ask on. Create keys.env with those values and retry.")
        print(f"\nNo usable {KEYS.name} found. Enter your API keys "
              "(input is hidden; press Enter to skip an optional one).\n")
        for name, purpose in REQUIRED_KEYS.items():
            if values.get(name) or (name == "OPENAI_API_KEY" and openai_transport.resolve_primary(values)):
                continue
            group = next((g for g in ESSENTIAL_GROUPS if name in g), ())
            if any(values.get(other) for other in group):
                continue                                  # group already satisfied
            entered = getpass.getpass(f"  {name}  -- {purpose}\n  > ").strip()
            if entered:
                values[name] = entered
            elif group and name == group[-1]:
                raise SystemExit(
                    f"{' or '.join(group)} is required; nothing to run without it.")
        body = "\n".join(f"{k}={v}" for k, v in values.items() if v) + "\n"
        KEYS.write_text(body)
        KEYS.chmod(stat.S_IRUSR | stat.S_IWUSR)          # owner read/write only
        print(f"\nWrote {KEYS} (permissions 600). It is gitignored.\n")

    if (sys.argv[1:2] and sys.argv[1] in CALIBRATION_COMMANDS and values.get("ANTHROPIC_API_KEY")
            and values.get("ANTHROPIC_AUTH_TOKEN")):
        # Both Anthropic credentials configured (2026-10-08): commands that run Fable calibration stay on
        # the API key and leave the OAuth token to authoring, so the two never share one rate limit
        # (twenty concurrent Fable episodes pushed every designer round into HTTP 429). Done here, not in
        # benchmark/harbor.py, whose bytes are the native runner identity every campaign records.
        values = {k: v for k, v in values.items() if k != "ANTHROPIC_AUTH_TOKEN"}
    return openai_transport.apply_file_credentials(values)


#: Subcommands that run native calibration episodes (screens; shipfix runs a screen and the release).
CALIBRATION_COMMANDS = ("screen", "shipfix")


def _run(module: str, *args: str) -> int:
    return subprocess.call([sys.executable, "-m", module, *args], cwd=ROOT)


def cmd_keys(args) -> int:
    values = load_keys(prompt_if_missing=not args.check)
    print(f"{KEYS}: {'present' if KEYS.is_file() else 'absent'}")
    for name, purpose in REQUIRED_KEYS.items():
        state = "set" if values.get(name) else "not set"
        flag = "" if name in ESSENTIAL else "  (optional)"
        print(f"  {name:22s} {state}{flag}   {purpose}")
    missing = _unsatisfied(values)
    for group in missing:
        print(f"  MISSING: {group}")
    return 0 if not missing else 1


def cmd_dump(args) -> int:
    """Show or set the trace dump this workspace works on."""
    from .config import dump_root

    if args.set:
        root = Path(args.set).expanduser().resolve()
        if not (root / "sessions").is_dir():
            print(f"{root} has no sessions/ directory -- that is not a trace dump.")
            return 2
        WORKSPACE.write_text(f'[workspace]\ndump = "{root}"\n')
        count = len([d for d in (root / "sessions").iterdir() if d.is_dir()])
        print(f"workspace dump set to {root}  ({count} sessions)")
        return 0

    try:
        root = dump_root()
    except SystemExit as exc:
        print(exc)
        return 1
    sessions = [d for d in (root / "sessions").iterdir() if d.is_dir()]
    source = "ARENA_DUMP" if os.environ.get("ARENA_DUMP") else WORKSPACE.name
    print(f"dump    : {root}")
    print(f"set by  : {source}")
    print(f"sessions: {len(sessions)}")
    return 0


def cmd_select(args) -> int:
    load_keys()
    steps = [("pipeline.dump.index", []),
             ("pipeline.select.score", ["--include-inspiration"]),
             ("pipeline.select.priority", []),
             ("pipeline.select.classify", ["--need", str(args.need),
                                           "-j", str(args.workers)]),
             ("pipeline.select.shortlist", [])]
    for module, extra in steps:
        print(f"\n== {module.split('.')[-1]} ==")
        code = _run(module, *extra)
        if code:
            return code
    return 0


def cmd_build(args) -> int:
    """Phases 1-4: author the batch, grade it, screen it on both models, stop."""
    load_keys()
    extra = ["-n", str(args.count), "-o", args.out,
             "--task-workers", str(args.task_workers),
             "--rollout-budget", str(args.rollout_budget),
             "--max-tier", str(args.max_tier),
             "--screen-workers", str(args.screen_workers)]
    if args.ranks:
        extra += ["--ranks", args.ranks]
    if args.category:
        extra += ["--category", args.category]
    if args.no_calibrate:
        extra.append("--no-calibrate")
    if args.continue_stalled:
        extra.append("--continue-stalled")
    if getattr(args, "skip_built", False):
        extra.append("--skip-built")
    if getattr(args, "classified", None):
        extra += ["-c", args.classified]
    return _run("pipeline.orchestrate", *extra)


def cmd_review(args) -> int:
    """The build's semantic review over each bundle (pipeline/review_many.py)."""
    load_keys()
    return _run("pipeline.review_many", *args.paths, "--workers", str(args.workers))


def cmd_genuine(args) -> int:
    """Certify every counted failure of each bundle's current screen (pipeline/genuine.py)."""
    return _run("pipeline.genuine", *args.paths, *(["--regrade"] if args.regrade else []))


def cmd_shipcheck(args) -> int:
    """The ship-time sweep of Arena's review classes (pipeline/shipcheck.py)."""
    return _run("pipeline.shipcheck", *args.paths, *(["--runs", args.runs] if args.runs else []),
                *(["--image"] if args.image else []), *(["--repro"] if args.repro else []),
                *(["--write"] if args.write else []))


def cmd_critic(args) -> int:
    """Counter-implementations of published clauses, graded by the shipped grader (pipeline/coverage_critic.py)."""
    load_keys(prompt_if_missing=False)
    return _run("pipeline.coverage_critic", *args.paths, "--workers", str(args.workers),
                *(["--model", args.model] if args.model else []))


def cmd_shipfix(args) -> int:
    """Deterministic ship fixes on a new version, then acceptance (pipeline/shipfix.py)."""
    load_keys(prompt_if_missing=False)
    if args.release:
        return _run("pipeline.shipfix", args.version, "--release")
    return _run("pipeline.shipfix", args.version, *(["--edits", args.edits] if args.edits else []),
                *(["--finish"] if args.finish else []), *(["--no-screen"] if args.no_screen else []))


def cmd_reopen(args) -> int:
    """Grant a review-blocked build one more repair pass (pipeline/reopen.py)."""
    return _run("pipeline.reopen", *args.paths, *(["--why", args.why] if args.why else []))


def cmd_screen(args) -> int:
    """Phase 4 on its own: the 5 x 2 calibration screen, or its report."""
    if not args.report_only:
        load_keys()
    extra = [*args.paths, "--task-workers", str(args.task_workers)]
    if args.report_only:
        extra.append("--report-only")
    if args.include_unvalidated:
        extra.append("--include-unvalidated")
    if args.relaunch_incomplete:
        extra.append("--relaunch-incomplete")
    if args.out:
        extra += ["-o", args.out]
    if args.label:
        extra += ["--label", args.label]
    return _run("pipeline.screen", *extra)


def cmd_status(args) -> int:
    print(f"workspace : {ROOT}")
    try:
        from .config import dump_root
        root = dump_root()
        n = len([d for d in (root / "sessions").iterdir() if d.is_dir()])
        print(f"dump      : {root}  ({n} sessions)")
    except SystemExit:
        print("dump      : NOT SET -- run: ./arena dump --set /path/to/dump")
    keys = load_keys(prompt_if_missing=False)
    print(f"keys.env  : {'present' if KEYS.is_file() else 'ABSENT -- run: arena keys'}"
          + ("" if not KEYS.is_file() else
             f"  ({', '.join(k for k in REQUIRED_KEYS if keys.get(k)) or 'no keys set'})"))

    def load(name: str):
        path = ARTIFACTS / name
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None

    print("\n-- selection --")
    index = load("index.json")
    print(f"  index      : {index['sessions']} sessions, {index['seeds']} seeds"
          if index else "  index      : not built   (arena select)")
    cand = load("candidates.json")
    if cand:
        c = cand["counts"]
        print(f"  candidates : {c['passed_kill_filters']} past floors, "
              f"{c['replay_ready']} replay-ready, {c['sessions']} sessions")
    tiered = load("tiered.json")
    if tiered:
        print("  tiers      : " + "  ".join(f"T{k}={v}"
                                            for k, v in tiered["counts"].items()))
    clsf = load("classified.json")
    if clsf:
        survivors = [r for r in clsf["rows"] if r["classification"].get("survives")]
        depth = clsf.get("depth") or {}
        print(f"  classified : {len(clsf['rows'])} seeds -> {len(survivors)} survive"
              + (f", descended to tier {depth.get('reached_tier')}" if depth else ""))

    print("\n-- environments --")
    batch = load("batch-build.json")
    if batch:
        print(f"  last batch : {batch['tasks']} tasks in {batch['wall_clock_s']}s "
              f"({batch['throughput_per_hour']}/h), "
              f"budget peak {batch['rollout_budget']['peak_in_flight']}"
              f"/{batch['rollout_budget']['size']}")
        print(f"  status     : {batch['status_counts']}")
        print(f"  creation   : {batch.get('creation_counts', 'unrecorded')}")
        for r in batch["results"]:
            mark = {"green": "OK  ", "blocked": "BLOK", "unverified": "UNVR",
                    "error": "ERR "}.get(r["status"], "?   ")
            detail = r["error"] or (r["blockers"][0] if r["blockers"] else "")
            print(f"    {mark} {r['slug']:38s} t{r['tier']} "
                  f"{r['stage_reached']:10s} {detail[:52]}")
    else:
        print("  last batch : none   (arena build -n 3)")

    from .config import bundle_paths
    from .artifacts import identity, policy_identity
    bundles = sorted(set(bundle_paths(ROOT)) | {b for tree in ROOT.glob("envs*")
                     if tree.is_dir() for b in bundle_paths(tree)})
    policy = policy_identity()
    print(f"\n-- bundles on disk: {len(bundles)} --")
    for bundle in bundles:
        probes = bundle / "verification" / "probes.json"
        ledger = bundle / "provenance" / "ledger.json"
        bits = []
        try:
            release = json.loads((bundle / "verification/release.json").read_text())
            current = release.get("artifact_sha256") == identity(bundle) and release.get("policy_sha256") == policy
            bits.append(release["status"] if current else "release evidence stale")
        except (OSError, ValueError, KeyError):
            bits.append("not release-qualified")
        if ledger.is_file():
            try:
                provenance = json.loads(ledger.read_text())
                bits.append(f"source fidelity {provenance['faithfulness_by_bytes']:.0%}")
                if "exact_copy_by_bytes" in provenance:
                    bits.append(f"exact copies {provenance['exact_copy_by_bytes']:.0%}")
            except Exception:
                pass
        if probes.is_file():
            try:
                g = json.loads(probes.read_text())["results"].get("golden", {})
                bits.append(f"golden {g.get('reward')}")
            except Exception:
                pass
        cal = sorted((bundle / "verification").glob("calibration-*.json")) \
            if (bundle / "verification").is_dir() else []
        if cal:
            # A calibration describes the grader that produced it. If
            # tests/checks.json has moved since -- a rebalance, a reweighting, a
            # redesign -- the stored mean measures a task that no longer exists,
            # and printing it unmarked is how a stale number gets read as a
            # current one. The whole batch went stale at once here when weights
            # were normalised after calibrating.
            checks_file = bundle / "tests" / "checks.json"
            try:
                measured = json.loads(cal[-1].read_text())
                stale = measured.get("grader_fingerprint") != identity(bundle)
                summary = measured["summary"]
                bits.append(
                    f"mean {summary['mean_reward']} ({summary['valid']} valid)"
                    + ("  [STALE: task identity differs]" if stale else ""))
            except Exception:
                pass
        print(f"  {bundle.name:42s} {'  '.join(bits) or 'not yet verified'}")
    return 0


def cmd_verify(args) -> int:
    """Stage I3 -- the shipped grader, with no repository on the path."""
    extra = ([args.root] if args.root else []) + (["-o", args.out] if args.out else [])
    return _run("pipeline.verify_shipped", *extra)


def cmd_categories(args) -> int:
    """Stage D2 -- classify surviving seeds into target faithfulness levels F3-F0."""
    extra = (["-i", args.tiered] if args.tiered else []) + \
            (["-o", args.out] if args.out else [])
    return _run("pipeline.select.categories", *extra)


def cmd_harden(args) -> int:
    """Stage H -- raise a chosen task's difficulty. Never runs by itself."""
    load_keys(prompt_if_missing=bool(args.apply or getattr(args, "extend_steps", False)))
    extra = []
    if args.candidates:
        extra += ["--candidates", "--root", args.root or "envs"]
    if args.bundle:
        extra.append(args.bundle)
    if args.apply:
        extra.append("--apply")
    if args.compare:
        extra += ["--compare", args.compare]
    for idea in args.trap or []:
        extra += ["--trap", idea]
    if args.traps:
        extra += ["--traps", str(Path(args.traps).resolve())]
    if args.no_screen:
        extra.append("--no-screen")
    if getattr(args, "attempt_budget", None) is not None:
        extra += ["--attempt-budget", str(args.attempt_budget)]
    if getattr(args, "extend_steps", False):
        extra.append("--extend-steps")
    if getattr(args, "fast", False):
        extra.append("--fast")
    if getattr(args, "review_repair", False):
        extra.append("--review-repair")
    if getattr(args, "repair_root", False):
        extra.append("--repair-root")
    for fix in getattr(args, "fix", None) or []:
        extra += ["--fix", fix]
    if getattr(args, "reopen_stopped", False):
        extra.append("--reopen-stopped")
    if getattr(args, "out", None):
        extra += ["--out", args.out]
    return _run("pipeline.harden", *extra)


def cmd_benchmark(args) -> int:
    """Stage J -- run a delivered bundle through Harbor, in Docker.

    Everything before this grades the package in our own harness. This runs it
    where it actually ships: `schema_version = "1.4"`, `/app/deliverables`,
    `/app/reward.json` are a Harbor contract the pipeline had never executed.
    A bundle that is green here and broken there is the failure mode this stage
    exists to close.
    """
    from .benchmark import harbor

    if args.doctor:
        report = harbor.doctor()
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    if not args.bundle:
        raise ValueError("benchmark requires a bundle or --doctor")
    from .benchmark.preflight import validate_task
    validation = validate_task(Path(args.bundle), legacy_diagnostic=args.legacy_diagnostic)
    print(f"  Task mode: {validation['mode']}. {validation['note']}")

    if not args.dry_run:
        health = harbor.doctor()
        if not health["ok"]:
            for problem in health["errors"]:
                print(f"  {problem}")
            print("\nNative preflight failed; resolve the reported runtime problems.")
            return 2
        if args.agent in {"terminus-2", "claude-code", "codex", "harness"}:
            load_keys()
    out = Path(args.out) if args.out else (ARTIFACTS / "harbor" / Path(args.bundle).name)
    # A model campaign is calibration, and calibration runs only on Coder. The
    # free deterministic agents (oracle, nop, fixture) may run locally, or on
    # Coder with --coder to check the remote setup.
    runner = harbor.run_campaign
    # A model campaign is calibration, and calibration runs on Coder by default:
    # the pool gives every episode the same rated machine, which is what makes
    # rates from different bundles comparable.
    #
    # `ARENA_NATIVE_BACKEND=local` is an explicit, opt-in escape for the case
    # where the pool cannot help. Measured here: every workspace the pool asked
    # for sat "Queued" on the deployment's provisioner, so campaigns blocked in
    # acquire() indefinitely and nothing was measured at all. A refusal that
    # cannot be satisfied is worse than a stated caveat -- but the caveat is
    # real, so the report records which backend ran.
    local_override = os.environ.get("ARENA_NATIVE_BACKEND") == "local"
    if args.agent == "harness":
        # The calibration harness assigned to the model (Fable 5.1 -> Claude
        # Code, GPT 6 Astra -> Codex), user 2026-09-26.
        args.agent = policy.harness_for(args.model)
    if not args.dry_run and (args.agent in {"terminus-2", "claude-code", "codex"} or args.coder) \
            and not local_override:
        from .benchmark.coder import run_campaign as runner
    extra = {"fixture_case": Path(args.fixture_case)} if args.fixture_case else {}
    report = runner(
        Path(args.bundle), out, model=args.model, agent=args.agent,
        rollouts=args.rollouts, workers=args.workers, effort=args.effort,
        no_limits=args.no_limits, max_output_tokens=args.max_output_tokens,
        context_limit=args.context_limit,
        legacy_diagnostic=args.legacy_diagnostic,
        dry_run=args.dry_run, **extra)
    summary = report.get("summary") or {}
    print(f"  valid {summary.get('valid')}/{summary.get('requested')}  "
          f"void {summary.get('void')}  mean {summary.get('mean_reward')}")
    return 0 if not report.get("errors") else 1


def cmd_coder(args) -> int:
    """The Coder pool (status, login, stop, delete) and remote orchestration
    (prepare, run, runs, pull, watch, kill: `benchmark/coder_run.py`)."""
    if args.action in ("status", "login", "stop", "delete"):
        from .benchmark.coder import main as coder_main
        return coder_main([args.action])
    from .benchmark.coder_run import main as run_main
    return run_main([args.action, *args.rest])


def cmd_deliverable(args) -> int:
    """Assemble the batch Arena receives, and refuse a batch that fails its own
    acceptance criteria rather than letting Arena compute that first."""
    from .deliverable import build, render_markdown

    sessions = json.loads(Path(args.sessions).read_text())
    if args.excluded == "auto":
        from .accounting import reasons, summary
        artifacts = Path(args.artifacts)
        excluded = reasons(sessions, artifacts)
        counts = summary(sessions, artifacts)
        print(f"derived exclusions for {counts['sessions']} session(s) from "
              f"{artifacts}: " + ", ".join(f"{n} {c}" for c, n in
                                           counts["by_code"].items()))
    elif args.excluded:
        excluded = json.loads(Path(args.excluded).read_text())
    else:
        excluded = {}
    # Trace accounting checks the shared list against the dump's own session
    # count, read from the same artifacts the exclusions came from.
    report = build(Path(args.root), sessions, excluded,
                   Path(args.out) if args.out else None,
                   index=Path(args.artifacts) / "index.json")
    print(render_markdown(report))
    if not report["acceptance"]["ok"]:
        print("\nBatch does NOT meet the acceptance criteria above.")
        return 1
    return 0


def cmd_deliver(args) -> int:
    """File a working tree into its faithfulness-level delivery folders."""
    from .deliver import main as deliver_main

    argv = [args.root, "-o", args.out]
    if args.accepted_only:
        argv.append("--accepted-only")
    if args.dry_run:
        argv.append("--dry-run")
    return deliver_main(argv)


def cmd_tasks_check(args) -> int:
    """`harbor tasks-check`, which the spec makes a 100%-of-tasks criterion."""
    from .benchmark import tasks_check

    worst = 0
    for bundle in _bundles(Path(args.root)):
        record = tasks_check.run(bundle)
        print(f"{record['status']:>10}  {bundle.name}")
        if record["status"] == "fail":
            worst = max(worst, 1)
        elif record["status"] != "pass":
            worst = max(worst, 2)
    return worst


def cmd_analyse(args) -> int:
    """Phase 4's read of a calibration: where the model failed, and which
    checks measured nothing. Diagnosis -- it never rejects a task for hardness."""
    from .phases import analyse

    report = analyse(Path(args.bundle))
    print(json.dumps(report, indent=2))
    return 0 if report.get("available") else 2


def _bundles(root: Path):
    """Every bundle under `root` by `config.bundle_paths`, or `root` itself.

    A bundle that matches the naming scheme but lacks `task.toml` is still
    listed: a sweep that skipped it would report a clean result it never ran.
    """
    from .config import bundle_paths
    found = set(bundle_paths(root))
    if (root / "task.toml").is_file():
        found.add(root)
    return sorted(found)


def cmd_gate(args) -> int:
    return _run("pipeline.run", *args.bundles, *(["-v"] if args.verbose else []))


def cmd_sandbox(args) -> int:
    """Run a bundle's grader in Docker exactly as Harbor would, without a model.

    `--controls` executes the whole control suite (nop, golden, alternative,
    every mutant, empty, destructive, test_tamper) and writes the receipt;
    `--golden` grades the reference fix alone; `--shell CMD` runs one command in
    the agent image over the fixture and prints what it did.
    """
    from .author import controls as controls_stage
    from .author import probes as probe_stage
    from .author import sandbox

    ok, why = sandbox.available()
    if not ok:
        print(f"sandbox unavailable: {why}")
        return 2
    bundle = Path(args.bundle).resolve()
    if args.shell:
        result = sandbox.shell(bundle, args.shell)
        sys.stdout.write(result.get("stdout") or "")
        sys.stderr.write(result.get("stderr") or "")
        return int(result.get("exit_code") or 0)
    if args.golden:
        record = sandbox.verify(bundle, {s: sandbox.solve_variant(bundle, s) for s in sandbox._steps(bundle)})
        print(json.dumps({s: {"reward": r.get("reward"), "valid": r.get("valid")}
                          for s, r in record["steps"].items()}, indent=2))
        return 0 if all(r.get("reward") == 1.0 for r in record["steps"].values()) else 1
    receipt = controls_stage.run(bundle, workers=getattr(args, "control_workers", 1))
    probes = probe_stage.run(bundle, controls_receipt=receipt)
    print(f"controls: {'ok' if receipt['ok'] else 'FAILED'} in {receipt['seconds']}s")
    for err in receipt["errors"]:
        print(f"  ERROR    {err}")
    for adv in receipt.get("advisories") or []:
        print(f"  advisory {adv}")
    print(f"probes  : {'ok' if probes.get('ok') else 'FAILED'}")
    for name, res in (probes.get("results") or {}).items():
        if isinstance(res, dict) and "ok" in res:
            print(f"  {'ok ' if res['ok'] else 'BAD'} {name:18s} reward={res.get('reward')}")
    return 0 if receipt["ok"] and probes.get("ok") else 1


def cmd_release(args) -> int:
    extra = []
    if args.command == "qualify":
        extra = ["--qualify", "--models", *args.models, "--review-model", args.review_model,
                 "--blind-model", args.blind_model, "--native-root", args.native_root,
                 "--rollouts", str(args.rollouts), "--workers", str(args.workers),
                 "--repair-attempts", str(args.repair_attempts)]
    return _run("pipeline.release", args.bundle, *extra)


def build_parser():
    """The real `arena` parser, built once so tests cannot drift from it.

    A test that hand-rolls a `SimpleNamespace` of arguments is testing its own
    stub, not the CLI: when a subcommand grew a `--fixture-case` flag, the stub
    kept passing until the command read the attribute and raised
    `AttributeError` at runtime. Building args through this parser means a new
    flag reaches the tests the moment it is added.
    """
    import argparse
    ap = argparse.ArgumentParser(
        prog="arena", description="Arena environment pipeline")
    sub = ap.add_subparsers(dest="command", required=True)

    k = sub.add_parser("keys", help="check or set up API credentials")
    k.add_argument("--check", action="store_true", help="report without prompting")
    k.set_defaults(fn=cmd_keys)

    dp = sub.add_parser("dump", help="show or set the trace dump to work on")
    dp.add_argument("--set", metavar="PATH", help="point this workspace at a dump")
    dp.set_defaults(fn=cmd_dump)

    s = sub.add_parser("select", help="index the dump and shortlist candidates")
    s.add_argument("--need", type=int, default=6,
                   help="stop classifying once this many seeds survive")
    s.add_argument("-j", "--workers", type=int, default=8)
    s.set_defaults(fn=cmd_select)

    b = sub.add_parser("build", help="author and grade a batch, then screen it on both "
                                     "calibration models and stop")
    b.add_argument("-n", "--count", type=int, default=3)
    b.add_argument("-o", "--out", default="envs")
    b.add_argument("--task-workers", type=int, default=_config.MAX_TASK_WORKERS)
    b.add_argument("--rollout-budget", type=int, default=_config.MAX_ROLLOUT_BUDGET)
    b.add_argument("--max-tier", type=int, default=4)
    b.add_argument("--category", default=None,
                   help="build only seeds classified to these target faithfulness "
                        "levels (F3 Replay, F2 Scenario, F1 Pattern, F0 Inspired), "
                        "e.g. F3 or F3,F2")
    b.add_argument("--ranks", default=None,
                   help="comma-separated 1-based ranks to build instead of the top -n")
    b.add_argument("--no-calibrate", action="store_true",
                   help="stop after authoring and grading; spend no solver episodes")
    b.add_argument("--screen-workers", type=int, default=_config.MAX_QUALIFY_WORKERS,
                   help="tasks screened concurrently once the batch is built")
    b.add_argument("--continue-stalled", action="store_true",
                   help="continue a design that ran out of steps from its retained draft")
    b.add_argument("--skip-built", action="store_true",
                   help="drop seeds that already have a bundle in any envs* tree before ranking "
                        "(a new batch, and every Coder shard of it, builds only new material)")
    b.add_argument("-c", "--classified", default=None,
                   help="build from this classified-seeds file (a frozen wave snapshot) instead of "
                        "pipeline-artifacts/classified.json")
    b.set_defaults(fn=cmd_build)

    rv = sub.add_parser("review", help="run the independent semantic review a build runs, on any bundle "
                        "(hardening revisions included)")
    rv.add_argument("paths", nargs="+", help="bundles, or trees of bundles")
    rv.add_argument("--workers", type=int, default=4)
    rv.set_defaults(fn=cmd_review)

    gn = sub.add_parser("genuine", help="certify that every counted failure of a screen is the model's: "
                        "assertion or the submission's own crash, reproduced on re-grade, reference and "
                        "alternative passing, a published sentence behind each check")
    gn.add_argument("paths", nargs="+", help="bundles, or trees of bundles")
    gn.add_argument("--regrade", action="store_true", help="re-grade each failing episode's captured tree")
    gn.set_defaults(fn=cmd_genuine)

    sk = sub.add_parser("shipcheck", help="ship-time sweep: Arena's review classes (stale grader, login-shell "
                        "PATH, missing deps, docs vs toolchain, unshipped paths, undeclared omissions, false "
                        "refusals, over-strict guardrails, certificate coverage, coverage gaps); each finding "
                        "names its fix route and the re-measurement the fix needs")
    sk.add_argument("paths", nargs="+", help="bundles")
    sk.add_argument("--runs", help="a delivered runs/ folder (default <bundle>/runs)")
    sk.add_argument("--image", action="store_true", help="probe the built agent image: shells, imports, packages")
    sk.add_argument("--repro", action="store_true", help="rebuild both images from the package (--no-cache) and grade")
    sk.add_argument("--write", action="store_true", help="write verification/shipcheck.json")
    sk.set_defaults(fn=cmd_shipcheck)

    cr = sub.add_parser("critic", help="coverage critic: a model writes wrong implementations of published "
                        "clauses on purpose; each is graded by the shipped grader and a 1.0 is a gap")
    cr.add_argument("paths", nargs="+", help="bundles")
    cr.add_argument("--workers", type=int, default=3)
    cr.add_argument("--model", default=None)
    cr.set_defaults(fn=cmd_critic)

    sf = sub.add_parser("shipfix", help="apply named environment fixes (login-shell PATH, pinned packages, "
                        "README/toolchain lines, script guards, guardrail repair, provenance notes) to a NEW "
                        "version without a model, rebuild it, then run controls, gates, review, one 5+5 screen, "
                        "the genuine audit, the coverage critic and the ship-time sweep")
    sf.add_argument("version")
    sf.add_argument("--edits", help="the edits as a JSON object (required unless --release)")
    sf.add_argument("--finish", action="store_true")
    sf.add_argument("--no-screen", action="store_true")
    sf.add_argument("--release", action="store_true",
                    help="on a final version: revalidate stale controls, qualify 10 x 2, certify the "
                         "calibration campaigns, sweep")
    sf.set_defaults(fn=cmd_shipfix)

    ro = sub.add_parser("reopen", help="grant each review-blocked build ONE more repair pass with its review "
                        "findings (user decision 2026-10-04); the grant and the pass are recorded")
    ro.add_argument("paths", nargs="+", help="bundles, or trees of bundles")
    ro.add_argument("--why", help="the decision being recorded")
    ro.set_defaults(fn=cmd_reopen)

    sc = sub.add_parser("screen",
                        help=f"the calibration screen: {policy.SCREEN_EPISODES_PER_MODEL} "
                             "episodes per model on both calibration models, with "
                             "pass@1, pass@4, mean reward and a hardening outlook")
    sc.add_argument("paths", nargs="+", help="bundles, or trees of bundles")
    sc.add_argument("--report-only", action="store_true",
                    help="read the retained screens; launch nothing")
    sc.add_argument("--task-workers", type=int, default=_config.MAX_QUALIFY_WORKERS)
    sc.add_argument("--label", default=None, help="the occasion, recorded in the receipt")
    sc.add_argument("--include-unvalidated", action="store_true",
                    help="also screen bundles whose gate chain is not green")
    sc.add_argument("--relaunch-incomplete", action="store_true",
                    help="replace a campaign still short of valid episodes after its void "
                         "top-ups with a fresh one (re-runs scored episodes; recorded)")
    sc.add_argument("-o", "--out", help="JSON report path (a .md is written beside it)")
    sc.set_defaults(fn=cmd_screen)

    research = sub.add_parser("research", help="prepare, validate, run and report frozen decoy experiments or fresh confirmation")
    research.add_argument("args", nargs=argparse.REMAINDER)
    research.set_defaults(fn=lambda args: _run("pipeline.research", *args.args))

    st = sub.add_parser("status", help="what has been built and verified")
    st.set_defaults(fn=cmd_status)

    di = sub.add_parser("diagnose", help="reconcile unique checks, raw episodes and validity without model calls")
    di.add_argument("root")
    di.add_argument("-o", "--out", required=True)
    di.set_defaults(fn=lambda args: _run("pipeline.diagnose", args.root, "-o", args.out))

    bm = sub.add_parser("benchmark", help="run a bundle through Harbor in Docker")
    bm.add_argument("bundle", nargs="?", help="bundle directory")
    bm.add_argument("--doctor", action="store_true", help="check Harbor/Docker only")
    bm.add_argument("-m", "--model", default=policy.resolve_calibration_models()[0])
    bm.add_argument("--agent", default="harness",
                    choices=["harness", "claude-code", "codex", "terminus-2", "oracle", "nop", "fixture"],
                    help="harness (default): the calibration harness assigned to --model -- "
                         "Claude Code for Fable 5.1, Codex for GPT 6 Astra")
    bm.add_argument("-r", "--rollouts", type=int, default=1)
    bm.add_argument("-j", "--workers", type=int, default=1)
    bm.add_argument("--effort", default=policy.SPEC_CALIBRATION_EFFORT,
                    help="reasoning effort; the spec's calibration effort by default")
    bm.add_argument("--limits", dest="no_limits", action="store_false",
                    default=policy.SPEC_NO_LIMITS,
                    help="impose the legacy 40-turn and 4-hour limits (diagnostics only; "
                         "calibration runs without limits)")
    bm.add_argument("--no-limits", dest="no_limits", action="store_true",
                    help=argparse.SUPPRESS)
    bm.add_argument("--max-output-tokens", type=int)
    bm.add_argument("--context-limit", type=int)
    bm.add_argument("--legacy-diagnostic", action="store_true",
                    help="explicitly run historical task semantics; never production qualification")
    bm.add_argument("--coder", action="store_true",
                    help="run oracle/nop/fixture on the Coder pool too (model runs always do)")
    bm.add_argument("--fixture-case", metavar="DIR",
                    help="scripted agent case for --agent fixture")
    bm.add_argument("-o", "--out")
    bm.add_argument("--dry-run", action="store_true")
    bm.set_defaults(fn=cmd_benchmark)

    cat = sub.add_parser("categories",
                         help="classify seeds into target levels F3 Replay / F2 Scenario / F1 Pattern / F0 Inspired")
    cat.add_argument("-i", "--tiered", help="tiered.json to read")
    cat.add_argument("-o", "--out", help="where to write categorised.json")
    cat.set_defaults(fn=cmd_categories)

    h = sub.add_parser("harden",
                       help="raise a chosen task's difficulty (manual only; at most "
                            f"{policy.MAX_HARDENING_ATTEMPTS} redesign attempts per task, ever)")
    h.add_argument("bundle", nargs="?", help="the bundle to harden")
    h.add_argument("--compare", help="original bundle for a measured per-model comparison")
    h.add_argument("--candidates", action="store_true",
                   help="rank built tasks by how much is hardenable")
    h.add_argument("--root", help="where to look for bundles (with --candidates)")
    h.add_argument("--apply", action="store_true",
                   help="harden within the attempt budget: attempt 1 redesigns to be "
                        "extremely hard, later attempts repair or tighten; each valid "
                        "revision is screened (5 x 2). Default reports only")
    h.add_argument("--trap", action="append", metavar="IDEA",
                   help="a trap to add (repeatable); held to every legitimacy and "
                        "faithfulness rule, left out if the source cannot support it")
    h.add_argument("--traps", metavar="FILE",
                   help="JSON list of trap ideas: strings or {'idea', 'grounding'}")
    h.add_argument("--no-screen", action="store_true",
                   help="build the revision only; skip the round's "
                        f"{policy.SCREEN_EPISODES_PER_MODEL} x 2 calibration screen")
    h.add_argument("--attempt-budget", type=int, help="explicitly increase the recorded lifetime budget without resetting spend")
    h.add_argument("--extend-steps", action="store_true", help="create a separate lineage with appended checkpoints")
    h.add_argument("--fast", action="store_true",
                   help="hidden-only round in minutes: re-grade the captured episodes; a fresh screen confirms")
    h.add_argument("--reopen-stopped", action="store_true",
                   help="operator decision: reopen a lineage stopped by repeated findings (recorded)")
    h.add_argument("--fix", action="append", metavar="DEFECT",
                   help="a defect of the task the round must fix (repeatable); nothing else changes")
    h.add_argument("--review-repair", action="store_true",
                   help="repair the semantic review's validity findings on the lineage's latest measured "
                        "revision, reopening a target-met lineage; the repair is screened against the same bar")
    h.add_argument("--repair-root", action="store_true",
                   help="with --review-repair, operator decision: repair the lineage's ORIGINAL build itself "
                        "when it is the hard one and its later revisions measured easy (recorded)")
    h.add_argument("--out", help="fresh destination for --extend-steps")
    h.set_defaults(fn=cmd_harden)

    v = sub.add_parser("verify", help="run each bundle's grader exactly as it ships")
    v.add_argument("root", nargs="?", help="directory of bundles (default: envs/)")
    v.add_argument("-o", "--out", help="where to write the JSON report")
    v.set_defaults(fn=cmd_verify)

    g = sub.add_parser("gate", help="run the gate chain over bundles")
    g.add_argument("bundles", nargs="+")
    g.add_argument("-v", "--verbose", action="store_true")
    g.set_defaults(fn=cmd_gate)

    sb = sub.add_parser("sandbox", help="execute a bundle's grader in Docker without a model: "
                                        "the control suite, the reference fix, or one shell command")
    sb.add_argument("bundle")
    sb.add_argument("--golden", action="store_true", help="grade the reference fix only")
    sb.add_argument("--shell", metavar="CMD", help="run one command in the agent image over the fixture")
    sb.add_argument("--control-workers", type=int, choices=(1, 2, 3, 4), default=1,
                    help="independent control variants to execute concurrently (default: 1)")
    sb.set_defaults(fn=cmd_sandbox)

    r = sub.add_parser("release", help="verify all release evidence for this exact task version")
    r.add_argument("bundle")
    r.set_defaults(fn=cmd_release)
    q = sub.add_parser("qualify", help="the calibration of record (10 x 2) -- needs a clean 5 x 2 "
                                       "screen of this version; pass@1 never blocks it")
    q.add_argument("bundle")
    from .policy import resolve_calibration_models
    _primary, _secondary = resolve_calibration_models()
    q.add_argument("--models", nargs="+", default=[_primary, _secondary],
                   help="calibration models (spec: Fable 5.1 + GPT 6 Astra)")
    q.add_argument("--review-model", default=_secondary)
    q.add_argument("--blind-model", default=_secondary)
    q.add_argument("--native-root", default=str(ARTIFACTS / "native"))
    q.add_argument("--rollouts", type=int, default=10)
    q.add_argument("--workers", type=int, default=2)
    q.add_argument("--repair-attempts", type=int, default=0,
                   choices=range(policy.MAX_REPAIR_PASSES + 1),
                   help="archive, revise and requalify rejected candidates")
    q.set_defaults(fn=cmd_release)

    d = sub.add_parser("deliverable",
                       help="assemble the batch report, trace accounting and "
                            "per-environment reward-hacking notes")
    d.add_argument("root", help="delivery tree of environments")
    d.add_argument("--sessions", required=True,
                   help="JSON list of every session ID Arena shared (the "
                        "unfiltered yield denominator)")
    d.add_argument("--excluded",
                   help="JSON object mapping session -> reason, or 'auto' to "
                        "derive every reason from the selection artifacts")
    d.add_argument("--artifacts", default="pipeline-artifacts",
                   help="selection artifact directory: --excluded auto and the "
                        "dump session-count check read it")
    d.add_argument("-o", "--out")
    d.set_defaults(fn=cmd_deliverable)

    dv = sub.add_parser("deliver",
                        help="file a working tree into F3-replay/F2-scenario/"
                             "F1-pattern folders with a_f<n>_ names")
    dv.add_argument("root")
    dv.add_argument("-o", "--out", required=True)
    dv.add_argument("--accepted-only", action="store_true",
                    help="ship only tasks with a current accepted release receipt")
    dv.add_argument("--dry-run", action="store_true")
    dv.set_defaults(fn=cmd_deliver)

    tc = sub.add_parser("tasks-check",
                        help="run harbor's task validator over a bundle tree")
    tc.add_argument("root")
    tc.set_defaults(fn=cmd_tasks_check)

    cd = sub.add_parser("coder", help="the Coder pool calibration runs on, and whole-pipeline "
                                      "runs on Coder workspaces (prepare/run/runs/pull/watch/kill)")
    cd.add_argument("action", choices=("status", "login", "stop", "delete",
                                       "prepare", "run", "shard", "push", "runs", "pull", "watch", "kill"))
    cd.add_argument("rest", nargs=argparse.REMAINDER,
                    help="for run: <workspace> [--create] [--env K=V] -- <arena args>")
    cd.set_defaults(fn=cmd_coder)

    an = sub.add_parser("analyse",
                        help="read a calibration: failure attribution, dead and "
                             "unreachable checks")
    an.add_argument("bundle")
    an.set_defaults(fn=cmd_analyse)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except ValueError as exc:
        # Every refusal in the pipeline is a ValueError naming its reason. Show
        # the reason, not a traceback; ARENA_DEBUG=1 keeps the traceback.
        if os.environ.get("ARENA_DEBUG"):
            raise
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
