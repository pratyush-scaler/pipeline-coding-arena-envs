"""File a working tree into the shape Arena receives.

Two trees, deliberately, because they answer different questions:

    working tree    envs-fresh/rich/a-001-wordpress-site-audit
                    filed by SOURCE CATEGORY, which is known before authoring
                    starts and says which trace to attempt first.

    delivery tree   F2-scenario/a_f2_wordpress-site-audit
                    filed by MEASURED FAITHFULNESS, which is only knowable
                    after the build, from what actually survived into the task.

The build cannot create the delivery folder directly: it has to name the
directory before it has a ledger to measure, and a bundle filed as `F3-replay/`
that then measures F2 would be a directory lying about its contents. So filing
is a separate step that runs once the evidence exists.

It also does the work that only makes sense with the whole batch in view:
disambiguating names that collide. `a-002-brand-consulting` and
`a-003-brand-consulting` both reduce to `a_f2_brand-consulting`, and two tasks
cannot share a Harbor package name.

Nothing here decides whether a task is good. `release.py` does that, and a task
that is not accepted is filed with its blockers rather than quietly dropped --
an empty category is a finding, not something to hide.
"""
from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

from . import naming, policy
from .artifacts import ROOTS
from .ledger import faithfulness, levels


#: Where the delivery receipt sits. `verification/` is outside the artifact
#: identity, so writing it does not itself change what was accepted.
RECEIPT = "verification/delivery.json"

#: What a delivered task consists of -- an ALLOWLIST. A denylist shipped every
#: file nobody thought to exclude: authoring scratch (agent-probe,
#: solution-check, empty-check, extraction), an orchestrator error carrying a
#: local path and an HTTP body, probe work and campaign transcripts.
#:
#: The task package is the artifact identity's own roots (`artifacts.ROOTS`:
#: task.toml, steps, environment, tests, solution, provenance, the
#: reviewer-facing authoring/spec.json and traps.json, .dockerignore), so the
#: delivered package is exactly what release accepted.
PACKAGE_ROOTS = ROOTS

#: Verification receipts a reviewer reads. The repair receipts and the
#: qualification feedback are what REWARD-HACKING.md's "fixed" section is
#: rendered from, so they travel with the task.
RECEIPTS = (
    "verification/release.json",
    "verification/native-calibrations.json",
    "verification/native-controls.json",
    "verification/probes.json",
    "verification/controls.json",
    "verification/harbor-tasks-check.json",
    "verification/repair-disposition.json",
    "verification/repair-log.json",
    # The genuine-failure certificate and the ship-time sweep: what proves each
    # counted failure is the model's, and that Arena's review classes were
    # checked (user, 2026-10-07).
    "verification/genuine.json",
    "verification/shipcheck.json",
    "verification/shipcheck-waivers.json",
    "verification/coverage-critic.json",
)
RECEIPT_GLOBS = ("verification/qualification-revisions/*/feedback.json",)

#: Written into the delivered tree by delivery or `deliverable`, never copied
#: from a working bundle: a stale copy would describe another version.
PRODUCED = (RECEIPT, "runs", "REWARD-HACKING.md")


def delivered_files(bundle: Path) -> tuple[list[str], list[str]]:
    """(allowlisted files to deliver, problems). A symlink anywhere is a problem."""
    bundle = Path(bundle)
    base = bundle.resolve()
    files, problems = set(), []

    def take(path: Path) -> None:
        rel = path.relative_to(bundle).as_posix()
        if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            return
        if path.is_symlink() or path.resolve() != base / rel:
            problems.append(f"{rel} is or sits under a symlink; a delivered task holds regular files only")
        elif path.is_file():
            files.add(rel)

    for rel in (*PACKAGE_ROOTS, *RECEIPTS):
        root = bundle / rel
        if root.is_symlink():
            take(root)
        elif root.is_dir():
            for path in sorted(root.rglob("*")):
                take(path)
        elif root.exists():
            take(root)
    for pattern in RECEIPT_GLOBS:
        for path in sorted(bundle.glob(pattern)):
            take(path)
    return sorted(files - set(PRODUCED)), sorted(set(problems))


def working_bundles(root: Path) -> tuple[list[Path], list[str]]:
    """Built bundles under a working tree, and the bundle folders that are not.

    Discovery is `config.bundle_paths`, the one definition of where bundles
    live. A folder it finds with no `task.toml` is a half-built bundle; it is
    reported, never silently skipped.
    """
    from .config import bundle_paths

    found, problems = [], []
    for path in bundle_paths(root):
        if (path / "task.toml").is_file():
            found.append(path)
        else:
            problems.append(f"{path.name}: no task.toml; not a built bundle, not delivered")
    return found, problems


def source_acceptance(bundle: Path) -> dict:
    """Is this working-tree bundle accepted, as it stands on disk right now?

    `status == "accepted"` alone is not acceptance: the receipt must be bound
    to this exact task version, and to the policy the pipeline runs under now.
    """
    import hashlib

    from .artifacts import identity, policy_identity

    record = {"accepted": False, "problems": [], "release_sha256": None,
              "release_artifact_sha256": None, "release_policy_sha256": None,
              "source_identity": None}
    try:
        record["source_identity"] = identity(bundle)
    except (OSError, ValueError) as exc:
        record["problems"].append(f"the bundle has no identity: {exc}")
    path = bundle / "verification/release.json"
    if not path.is_file():
        record["problems"].append("no release receipt")
        return record
    try:
        raw = path.read_bytes()
        release = json.loads(raw)
    except (OSError, ValueError) as exc:
        record["problems"].append(f"unreadable release receipt: {exc}")
        return record
    record.update(release_sha256=hashlib.sha256(raw).hexdigest(),
                  release_artifact_sha256=release.get("artifact_sha256"),
                  release_policy_sha256=release.get("policy_sha256"))
    if release.get("status") != "accepted":
        record["problems"].append(f"release status is {release.get('status')!r}")
    if record["source_identity"] is None or release.get("artifact_sha256") != record["source_identity"]:
        record["problems"].append("release receipt is bound to another task version")
    if release.get("policy_sha256") is not None and release["policy_sha256"] != policy_identity():
        record["problems"].append("release was decided under another policy version; re-release")
    record["accepted"] = not record["problems"]
    return record


def plan(root: Path) -> dict:
    """What would be delivered where, without writing anything."""
    bundles, problems = working_bundles(root)
    assigned = {}
    for bundle in bundles:
        ledger_path = bundle / "provenance/ledger.json"
        if not ledger_path.is_file():
            problems.append(f"{bundle.name}: no ledger; cannot measure faithfulness")
            continue
        try:
            ledger = json.loads(ledger_path.read_text())
            assigned[bundle] = (ledger, levels.assign_measured(bundle, ledger))
        except Exception as exc:                  # noqa: BLE001
            problems.append(f"{bundle.name}: {type(exc).__name__}: {exc}")

    pairs = [(record["level"], bundle.name) for bundle, (_, record) in assigned.items()]
    names = naming.unique_names(pairs)
    clashes = naming.collisions(pairs)

    rows = []
    for bundle, (ledger, record) in sorted(assigned.items()):
        level = record["level"]
        rows.append({
            "source": str(bundle), "level": level,
            "level_source": record["level_source"],
            "short_name": names[bundle.name],
            "destination": (f"{policy.DELIVERY_DIRS[level]}/{names[bundle.name]}"
                            if level in policy.DELIVERY_DIRS else None),
            "fidelity": record["measured"]["fixture_fidelity_by_bytes"],
            "trace_derived_share": record["measured"]["trace_derived_share"],
            "calibration_reproduces_failure":
                record["measured"]["calibration_reproduces_failure"],
            "why_not_higher": record["why_not_higher"],
            "rejected": level == "F0" and not policy.SPEC_F0_ALLOWED,
            "reason": levels.problems(record)[0] if levels.problems(record) else None,
        })
    return {"schema": "arena-pipeline/delivery-plan/1", "rows": rows,
            "collisions": clashes, "problems": problems}


def deliver(root: Path, out: Path, *, accepted_only: bool = False) -> dict:
    """Copy every deliverable bundle into its faithfulness folder.

    `accepted_only` ships only tasks with a current accepted release receipt.
    The default ships everything that is not F0 and reports each one's release
    state, because a batch report has to describe what was built, not only what
    passed.
    """
    from .artifacts import atomic_json

    root, out = Path(root), Path(out)
    document = plan(root)
    delivered, held, refused = [], [], []

    # Fail closed on anything that would make the tree lie about its contents:
    # two bundles written to one destination (the second silently replaced the
    # first), or folders left from an earlier delivery that this one would not
    # produce (the batch report would count them).
    wanted = [r["destination"] for r in document["rows"] if r["destination"]]
    doubled = sorted({d for d in wanted if wanted.count(d) > 1})
    if doubled:
        raise ValueError(f"two bundles would be delivered to the same place: {doubled}. "
                         "Bundle directory names must be unique across the working tree")
    stale = sorted(str(p.relative_to(out)) for level_dir in policy.DELIVERY_DIRS.values()
                   for p in (out / level_dir).glob("*") if p.is_dir()
                   and str(p.relative_to(out)) not in wanted)
    if stale:
        raise ValueError(f"{out} holds {len(stale)} task(s) this delivery would not produce "
                         f"({', '.join(stale[:5])}); deliver into an empty directory")

    for row in document["rows"]:
        bundle = Path(row["source"])
        if row["rejected"]:
            held.append({**row, "held_because": row["reason"]})
            continue

        acceptance = source_acceptance(bundle)
        row["accepted"] = acceptance["accepted"]
        row["acceptance_problems"] = acceptance["problems"]
        if accepted_only and not row["accepted"]:
            held.append({**row, "held_because": "no current accepted release receipt: "
                                                + "; ".join(acceptance["problems"])})
            continue

        files, copy_problems = delivered_files(bundle)
        if copy_problems:
            held.append({**row, "held_because": "; ".join(copy_problems)})
            continue
        destination = out / row["destination"]
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_toml = (bundle / "task.toml").read_bytes().decode("utf-8")
        source_ledger = (bundle / "provenance/ledger.json").read_bytes().decode("utf-8")
        for rel in files:
            target = destination / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundle / rel, target)

        # The ledger carries the level; the package name carries it too. The
        # level was measured on the source (calibration is bound to it), so
        # it is passed in rather than re-derived from the renamed copy.
        ledger = json.loads(source_ledger)
        previous_level = ledger.get("faithfulness_level")
        ledger = faithfulness.with_level(destination, ledger,
                                         row["calibration_reproduces_failure"])
        (destination / "provenance/ledger.json").write_text(
            json.dumps(ledger, indent=2))
        row["renamed"] = naming.apply(destination, row["level"])
        # Spec M4: the task carries its level too, not only the environment's
        # ledger. Derived from the accepted text by the same function
        # `deliverable.acceptance_problems` re-derives it with.
        try:
            (destination / "task.toml").write_text(naming.delivered_toml(
                source_toml, row["renamed"]["name"], row["level"]), encoding="utf-8")
        except (ValueError, TypeError) as exc:
            shutil.rmtree(destination)
            held.append({**row, "held_because": f"task.toml cannot carry its level: {exc}"})
            continue
        row["naming_problems"] = naming.problems(destination, row["level"])

        # Both rewrites are inside the artifact identity, so the release
        # receipt no longer matches the delivered bundle. The receipt below is
        # what lets `deliverable` prove the delivered bundle IS the accepted
        # one: it carries the accepted inventory and the two original files,
        # so the rename and the level are the only differences it will allow.
        from .artifacts import inventory
        try:
            source_inventory = inventory(bundle)
        except (OSError, ValueError):
            source_inventory = None
        atomic_json(destination / RECEIPT, {
            "schema": "arena-pipeline/delivery-receipt/2",
            "source": str(bundle),
            "source_identity": acceptance["source_identity"],
            "source_inventory": source_inventory,
            "source_task_toml": source_toml,
            "source_ledger": source_ledger,
            "release_sha256": acceptance["release_sha256"],
            "release_artifact_sha256": acceptance["release_artifact_sha256"],
            "release_policy_sha256": acceptance["release_policy_sha256"],
            "accepted_at_delivery": acceptance["accepted"],
            "acceptance_problems": acceptance["problems"],
            "rename": {"previous": tomllib.loads(source_toml)["task"]["name"],
                       "name": row["renamed"]["name"]},
            "level": {"previous": previous_level, "assigned": row["level"],
                      "calibration_reproduces_failure":
                          row["calibration_reproduces_failure"]},
            "metadata": {naming.LEVEL_KEY: row["level"]},
            "delivered_files": files,
        })

        # Spec P1, fail closed: the whole delivered task, exactly as it would
        # ship, and a finding anywhere refuses it. Locations and kinds only.
        from .deliverable import open_privacy, privacy_summary
        findings = open_privacy(destination)
        if findings:
            shutil.rmtree(destination)
            refused.append({"task": row["short_name"], "findings": findings})
            held.append({**row, "held_because": "refused: " + privacy_summary(findings),
                         "privacy": findings})
            continue
        delivered.append(row)

    record = {"schema": "arena-pipeline/delivery/1",
              "root": str(root), "out": str(out),
              "delivered": delivered, "held": held,
              "privacy_refused": refused,
              "collisions": document["collisions"],
              "problems": document["problems"],
              "by_level": {level: sum(1 for r in delivered if r["level"] == level)
                           for level in ("F3", "F2", "F1")}}
    out.mkdir(parents=True, exist_ok=True)
    atomic_json(out / "delivery.json", record)
    return record


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="File a working tree into faithfulness-level delivery folders")
    ap.add_argument("root", type=Path, help="working tree of built bundles")
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--accepted-only", action="store_true",
                    help="ship only tasks with a current accepted release receipt")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.dry_run:
        document = plan(args.root)
        for row in document["rows"]:
            print(f"  {row['level']}  {row['source']}\n"
                  f"       -> {row['destination'] or 'HELD (F0)'}")
        for name, group in sorted(document["collisions"].items()):
            print(f"  collision {name}: {', '.join(group)}")
        for problem in document["problems"]:
            print(f"  problem: {problem}")
        return 0 if not document["problems"] else 1

    record = deliver(args.root, args.out, accepted_only=args.accepted_only)
    print(f"delivered {len(record['delivered'])} bundle(s) to {args.out}")
    for level in ("F3", "F2", "F1"):
        if record["by_level"].get(level):
            print(f"  {policy.DELIVERY_DIRS[level]}: {record['by_level'][level]}")
    for row in record["held"]:
        print(f"  HELD {Path(row['source']).name}: {row['held_because']}")
    for problem in record["problems"]:
        print(f"  problem: {problem}")
    return 0 if record["delivered"] and not record["privacy_refused"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
