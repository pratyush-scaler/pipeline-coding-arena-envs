"""Deterministic ship fixes: the environment defects Arena's review found, applied without a model.

User, 2026-10-07: "Ensure all these issues are fixed properly", "no redesign", "fix the tasks
quickly". A designer round is the wrong tool for a README line, a pinned package or a PATH
export: it costs hours and may change what it was not asked to. This module makes exactly the
named edits to a NEW version of the task, rebuilds every generated file from the spec (the
current grader runtime included), re-measures the ledger, and then runs the pipeline's own
acceptance -- controls, probes, gates, review, ONE 5+5 screen, the genuine-failure audit, the
coverage critic and the ship-time sweep with the image probe and the from-scratch
reproduction. Nothing here decides validity; the receipts it produces do.

Edits (a JSON object; every one optional):

    login_shell_path      true -> spec.toolchain.login_shell_path         (C-002, C-032)
    python_deps           {name: version} -> spec.deps requirements.txt   (C-015)
    post_install_replace  [{old, new}] -> spec.toolchain.post_install     (C-033)
    suite_replace         [{path, old, new}] -> spec.suites[path]
    spec_text_replace     [{old, new, reason}] -> a literal replaced in EVERY string of the spec (suites, checks,
                          traps, notes): a privacy redaction the review preflight demands, e.g. a real address in test
                          data swapped for a reserved example.com one (c-008-flask, 2026-10-09)
    deliverables_remove   [path] -> a deliverable file contract dropped (a private helper the instruction does not
                          protect; c-028 v22 review C1, 2026-10-09). The last deliverable cannot be removed.
    suite_add             [{path, from_file, sha256}] -> a NEW hidden data file spec.suites['data/...'], read
                          from a file on the workspace (too large for one argv string) and checked by sha256
                          (c-003-firebase, 2026-10-09: the pinned Tailwind build the harness serves)
    check_replace         [{id, field, old, new}] -> one field of a check (e.g. runner.shell, basis): a
                          guardrail the review found over-strict, which hardening may not touch
    check_set             [{id, field, value}] -> one existing field of a check set to a JSON value
                          (e.g. runner): a guardrail moved onto another runner, nothing else changed
    post_install_append   [step] -> spec.toolchain.post_install
    deps_files            {manifest: text} -> spec.deps (a manifest and its lock, regenerated)
    waivers               [{check, subject, reason, at}] -> verification/shipcheck-waivers.json,
                          a human's decision only
    final_pass            true: the last round (its critic gaps stay open findings since 2026-10-09,
                          Arena feedback; they were declared untested before)
                          surfaces (user decision 2026-10-07), not another round
    fixture_replace       [{path, old, new, reason}] -> environment/repo  (C-020, C-036)
    fixture_add           [{path, from_file, sha256, reason}] -> a NEW fixture file the fixture references but never
                          shipped (Arena 2026-10-11 R07: /vendor/katex/*), read from a pinned file on the workspace
    deviations            [{what, why}] -> provenance/ledger.json         (C-034)

Fixture edits are declared in authoring/extraction/fixture-authored.json with their reason and
earn no source credit; the ledger also declares extraction exclusions and host paths
(C-028, C-030, C-036).

usage: python -m pipeline.shipfix <version> --edits '<json>' [--finish] [--no-screen]
       python -m pipeline.shipfix <final version> --release
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

SCHEMA = "arena-coding/shipfix/1"
RECEIPT = "verification/shipfix.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.split("[")[0].strip().lower())


def _replace_once(text: str, old: str, new: str, where: str) -> str:
    count = text.count(old)
    if count < 1:
        raise ValueError(f"{where}: the text to replace is not there: {old[:120]!r}")
    return text.replace(old, new)


def _check_field(spec: dict, check_id: str, field: str) -> tuple[dict, str]:
    """The mapping holding `field` (dotted) of the one check named `check_id`, and its last key."""
    found = [c for c in spec.get("checks") or [] if c.get("id") == check_id]
    found += [c for st in spec.get("steps") or [] if isinstance(st, dict)
              for c in st.get("checks") or [] if c.get("id") == check_id]
    if len(found) != 1:
        raise ValueError(f"checks: {check_id} is not exactly one check of this task")
    *parents, last = field.split(".")
    target = found[0]
    for key in parents:
        target = target[key]
    if last not in target:
        raise ValueError(f"checks/{check_id}: {field} is not a field of this check")
    return target, last


def edit_spec(spec: dict, edits: dict) -> list[str]:
    """Apply spec edits in place; return what changed."""
    done = []
    chain = spec.setdefault("toolchain", {})
    if edits.get("login_shell_path") and not chain.get("login_shell_path"):
        chain["login_shell_path"] = True
        done.append("toolchain.login_shell_path")
    for step in edits.get("post_install_append") or []:
        if step not in (chain.get("post_install") or []):
            chain["post_install"] = list(chain.get("post_install") or []) + [step]
            done.append("toolchain.post_install +1")
    for name, text in sorted((edits.get("deps_files") or {}).items()):
        spec.setdefault("deps", {})[name] = text
        done.append(f"deps/{name}")
    for item in edits.get("post_install_replace") or []:
        steps = list(chain.get("post_install") or [])
        new_steps = [s.replace(item["old"], item["new"]) for s in steps]
        if new_steps == steps:
            raise ValueError(f"toolchain.post_install: {item['old'][:120]!r} is not there")
        chain["post_install"] = new_steps
        done.append("toolchain.post_install")
    pins = edits.get("python_deps") or {}
    if pins:
        deps = spec.setdefault("deps", {})
        lines = (deps.get("requirements.txt") or "").splitlines()
        have = {_norm(re.split(r"[=<>!~ ;]", l.strip(), 1)[0]) for l in lines if l.strip() and not l.startswith("#")}
        added = [f"{name}=={version}" for name, version in sorted(pins.items()) if _norm(name) not in have]
        if added:
            deps["requirements.txt"] = "\n".join([l for l in lines if l.strip()] + added) + "\n"
            done.append("deps/requirements.txt: " + ", ".join(added))
    for item in edits.get("check_replace") or []:
        # A guardrail the semantic review found over-strict (c-072, 2026-10-08: g_no_grader_reads refused
        # inert documentation). Hardening may not change an untargeted guardrail, so the repair is a
        # named, exact edit here; controls, probes and the review then judge the result.
        target, last = _check_field(spec, item["id"], item["field"])
        target[last] = _replace_once(target[last], item["old"], item["new"], f"checks/{item['id']}.{item['field']}")
        done.append(f"checks/{item['id']}.{item['field']}")
    for item in edits.get("check_set") or []:
        # A guardrail moved onto another runner (c-072, 2026-10-08: as a `command` check it scanned the
        # test files an agent added, which pytest checks drop first, so an agent's own test with
        # `__import__(` zeroed the episode). The field is replaced whole; every other field is kept.
        target, last = _check_field(spec, item["id"], item["field"])
        target[last] = item["value"]
        done.append(f"checks/{item['id']}.{item['field']}")
    for item in edits.get("spec_text_replace") or []:
        old_text, new_text = str(item["old"]), str(item["new"])
        if not old_text:
            raise ValueError("spec_text_replace: empty text")
        count = 0

        def swap(value):
            nonlocal count
            if isinstance(value, str):
                count += value.count(old_text)
                return value.replace(old_text, new_text)
            if isinstance(value, list):
                return [swap(v) for v in value]
            if isinstance(value, dict):
                return {k: swap(v) for k, v in value.items()}
            return value

        for key in list(spec):
            spec[key] = swap(spec[key])
        if not count:
            raise ValueError(f"spec_text_replace: {old_text[:80]!r} is not in the spec")
        done.append(f"spec text {old_text[:40]!r} -> {new_text[:40]!r} ({count}x)")
    for item in edits.get("suite_add") or []:
        suites = spec.setdefault("suites", {})
        rel = str(item["path"])
        if not rel.startswith("data/") or ".." in Path(rel).parts:
            raise ValueError(f"suites: {rel} -- an added suite file is hidden data and lives under data/")
        if rel in suites:
            raise ValueError(f"suites: {rel} already exists; use suite_replace")
        data = Path(item["from_file"]).read_bytes()
        if _sha(data) != item["sha256"]:
            raise ValueError(f"suites: {rel}: {item['from_file']} does not match its pinned sha256")
        suites[rel] = data.decode("utf-8")
        done.append(f"suites/{rel} (+{len(data)} bytes, sha256 {item['sha256'][:12]})")
    for item in edits.get("suite_replace") or []:
        suites = spec.get("suites") or {}
        if item["path"] not in suites:
            raise ValueError(f"suites: {item['path']} is not a suite of this task")
        suites[item["path"]] = _replace_once(suites[item["path"]], item["old"], item["new"], f"suites/{item['path']}")
        done.append(f"suites/{item['path']}")
    for path in edits.get("deliverables_remove") or []:
        # A private helper kept as a hard file contract zeroes a behaviour-preserving refactor (c-028 v22 review C1,
        # 2026-10-09: accounts.py moved into a package failed every objective). The named entry goes; the public
        # entrypoint and any requested output stay.
        items = list(spec.get("deliverables") or [])
        kept = [d for d in items if not (isinstance(d, dict) and d.get("path") == path)]
        if len(kept) == len(items):
            raise ValueError(f"deliverables: {path} is not a deliverable of this task")
        if not kept:
            raise ValueError("deliverables: removing every deliverable leaves nothing to contract on")
        spec["deliverables"] = kept
        done.append(f"deliverables -{path}")
    return done


def edit_fixture(bundle: Path, edits: dict) -> dict[str, str]:
    """Apply fixture edits; return {rel: reason} for the declaration."""
    repo = Path(bundle) / "environment/repo"
    declared = {}
    for item in edits.get("fixture_replace") or []:
        path = repo / item["path"]
        text = _replace_once(path.read_text(encoding="utf-8"), item["old"], item["new"], item["path"])
        path.write_text(text, encoding="utf-8")
        declared[item["path"]] = item["reason"]
    for item in edits.get("fixture_add") or []:
        # A file the fixture references but never shipped (Arena 2026-10-11 R07: index.html loads /vendor/katex/*
        # that public/vendor never held). Read from a pinned file on the workspace, declared authored, no source
        # credit; an existing path is refused (that is fixture_replace's job).
        rel = str(item["path"])
        if rel.startswith("/") or ".." in Path(rel).parts or not rel:
            raise ValueError(f"fixture_add: {rel!r} must be a relative path inside the fixture")
        target = repo / rel
        if target.exists() or target.is_symlink():
            raise ValueError(f"fixture_add: {rel} already exists in the fixture")
        data = Path(item["from_file"]).read_bytes()
        if _sha(data) != item["sha256"]:
            raise ValueError(f"fixture_add: {item['from_file']} does not match its pinned sha256")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        declared[rel] = item["reason"]
    if declared:
        doc_path = Path(bundle) / "authoring/extraction/fixture-authored.json"
        doc = json.loads(doc_path.read_text()) if doc_path.is_file() else {"schema": "arena-coding/fixture-authored/1",
                                                                             "files": {}, "deleted": {}}
        for rel, reason in declared.items():
            doc.setdefault("files", {})[rel] = {"reason": reason, "sha256": _sha((repo / rel).read_bytes())}
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(json.dumps(doc, indent=2) + "\n")
    return declared


def update_ledger(bundle: Path, declared: dict[str, str], deviations: list[dict]) -> dict:
    """Re-measure the ledger from the fixture on disk without re-reading the trace.

    A file this round authored becomes `authored` (no source credit); every other
    entry keeps its recorded provenance. Extraction exclusions and host paths are
    declared. The level is then re-assigned from the re-measured figures.
    """
    from .ledger import faithfulness
    bundle = Path(bundle)
    path = bundle / "provenance/ledger.json"
    ledger = json.loads(path.read_text())
    repo = bundle / faithfulness.FIXTURE
    entries = {e["path"]: e for e in ledger.get("entries", [])}
    for rel in sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file()):
        raw = (repo / rel).read_bytes()
        entry = entries.get(rel)
        if entry is None or entry.get("sha256") != _sha(raw) or rel in declared:
            entries[rel] = {"path": rel, "origin": "authored", "sha256": _sha(raw), "bytes": len(raw),
                            "citation": None, "similarity": None,
                            "note": declared.get(rel) or (entry or {}).get("note") or "authored at ship-fix"}
    for rel in [r for r in entries if not (repo / r).is_file()]:
        del entries[rel]
    # The order faithfulness.build writes (Path order: `values/` before `values-night/`); the release
    # check compares the stored ledger with a rebuilt one list for list (c-019 v6, 2026-10-08).
    rows = [entries[k] for k in sorted(entries, key=lambda rel: repo / rel)]
    counts = {k: sum(1 for e in rows if e["origin"] == k) for k in faithfulness.ORIGINS}
    by_bytes = {k: sum(e["bytes"] for e in rows if e["origin"] == k) for k in faithfulness.ORIGINS}
    total, total_bytes = len(rows) or 1, sum(by_bytes.values()) or 1
    # Recovery's own omissions are kept; the declared ones are recomputed exactly as build does, so a
    # second file reading one host path is kept and a guarded-away one is dropped (c-019 v6, 2026-10-08).
    def declared_here(o) -> bool:
        reason = str(o.get("reason", "")) if isinstance(o, dict) else ""
        return reason.startswith("excluded_at_extraction:") or reason == "host_path_never_in_trace"
    omissions = [o for o in ledger.get("recovery_omissions") or [] if not declared_here(o)]
    omissions += faithfulness.extraction_exclusions(bundle) + faithfulness.host_paths(bundle)
    ledger.update({"entries": rows, "counts": counts, "bytes": by_bytes,
                   "faithfulness_by_file": faithfulness.share(counts),
                   "faithfulness_by_bytes": faithfulness.share(by_bytes),
                   "exact_copy_by_file": round(counts["lifted"] / total, 3),
                   "exact_copy_by_bytes": round(by_bytes["lifted"] / total_bytes, 3),
                   "recovery_omissions": omissions[:500]})
    if deviations:
        ledger["deviations_from_trace"] = list(ledger.get("deviations_from_trace") or []) + deviations
    ledger = faithfulness.with_level(bundle, ledger)
    path.write_text(json.dumps(ledger, indent=2))
    return ledger


def new_version(source: Path, edits: dict) -> Path:
    """A fresh revision of `source` with exactly `edits`, every generated file rebuilt."""
    from . import harden, naming
    from .author import build as build_mod
    source = Path(source).resolve()
    root = harden.lineage_root(source)
    with harden._lineage_lock(root):
        target = harden._next_revision(root, source)
    spec_path = target / "authoring/spec.json"
    spec = json.loads(spec_path.read_text())
    changed = edit_spec(spec, edits)
    declared = edit_fixture(target, edits)
    spec_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n")
    build_mod.build(spec, target)
    ledger = update_ledger(target, declared, edits.get("deviations") or [])
    naming.apply(target, ledger["faithfulness_level"])
    record = {"schema": SCHEMA, "from": str(source), "revision": str(target), "at": time.time(),
              "edits": edits, "spec_changed": changed, "fixture_authored": sorted(declared),
              "level": ledger["faithfulness_level"], "steps": {}}
    (target / "verification").mkdir(parents=True, exist_ok=True)
    (target / RECEIPT).write_text(json.dumps(record, indent=1))
    if edits.get("waivers"):
        # A human's decision, recorded as theirs (shipcheck reads only adjudicator "human").
        from .shipcheck import WAIVERS
        (target / WAIVERS).write_text(json.dumps({"adjudicator": "human", "waivers": edits["waivers"]}, indent=1))
    # Recorded in the lineage beside the hardening attempts, never as one: a ship fix
    # is not a redesign and spends no hardening budget.
    with harden._lineage_lock(root):
        ledger_doc = harden.read_ledger(root)
        ledger_doc.setdefault("shipfixes", []).append(
            {"revision": harden._rel(root, target), "from": harden._rel(root, source), "at": record["at"],
             "spec_changed": changed, "fixture_authored": sorted(declared)})
        harden._save(root, ledger_doc)
    return target


def review_model(version: Path) -> str:
    """The semantic reviewer: never the author's own model (release.qualify refuses that too)."""
    import os
    producer = (json.loads((Path(version) / "authoring/spec.json").read_text()).get("_producer") or {}).get("model", "")
    return os.environ.get("ARENA_REVIEW_MODEL") or ("claude-fable-5-1" if producer.startswith("gpt-") else "gpt-6-astra")


def finish(version: Path, *, screen: bool = True) -> dict:
    """The pipeline's own acceptance, in cost order; every step's outcome is recorded."""
    from . import phases, shipcheck
    from .author import controls, probes
    version = Path(version)
    receipt_path = version / RECEIPT
    record = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {"schema": SCHEMA, "steps": {}}

    lock = __import__("threading").Lock()

    def step(name, fn):
        started = time.time()
        try:
            out = fn()
            row = {"ok": True, "seconds": round(time.time() - started), "result": out}
        except Exception as exc:                        # noqa: BLE001 -- recorded, never swallowed silently
            row = {"ok": False, "seconds": round(time.time() - started),
                   "error": f"{type(exc).__name__}: {str(exc)[:600]}"}
        with lock:                                      # steps run on threads; dump under the same lock
            record["steps"][name] = row
            receipt_path.write_text(json.dumps(record, indent=1, default=str))
        return row

    # The screen and the critic need only the bundle, so they run beside the review (2026-10-09: the critic waited
    # ~15 min for the review on every final). They start once the controls are done: started beside the controls,
    # the critic's verifier-image build failed after 1 s on c-012 v23 and c-043 v24 while the controls built the
    # same images from the same tests/ (2026-10-09 ~21:50).
    import threading
    from . import coverage_critic
    screener = None
    if screen:
        from . import screen as screen_mod
        screener = threading.Thread(target=step, args=("screen", lambda: {"exit": screen_mod.main(
            [str(version), "--include-unvalidated", "--label", "shipfix", "--task-workers", "1"])}))

    def critic_step():
        # policy.CRITIC_SHIP_RULE (user, 2026-10-09: "Closed-and-verified"): re-execute the lineage's last
        # searching run on this version; a fresh search only where none is usable. Gaps stay open findings
        # (Arena feedback 2026-10-08: declared gaps shipped as executed 1.0 counter-implementations).
        try:
            result = coverage_critic.ship_critic(version, record_dir=version / "verification")
        except Exception as exc:                        # noqa: BLE001 -- an image build that lost a race is retried once
            if "docker build" not in str(exc):
                raise
            time.sleep(30)
            result = coverage_critic.ship_critic(version, record_dir=version / "verification")
        return {"gaps": len(result["gaps"]), "declared": False, "mode": result.get("mode", "search")}

    critic_thread = threading.Thread(target=step, args=("critic", critic_step))

    step("controls", lambda: {"ok": controls.run(version, workers=3).get("ok")})
    if screener is not None:
        screener.start()
    critic_thread.start()
    step("probes", lambda: {"ok": probes.run(version).get("ok")})
    reviewer = review_model(version)
    def grade_once():
        result = phases.grade(version, review_model=reviewer)
        return {"ok": result.ok, "findings": result.findings[:20]}

    def grade_with_retry():
        # A review that could not START (provenance recomputed against a trace
        # cache another process was writing, a provider error) measured
        # nothing; c-031 (2026-10-07) passed the same check minutes later.
        first = grade_once()
        # Also infrastructure: the harbor tasks-check agent timed out installing itself (2026-10-09:
        # AgentSetupTimeoutError after 360 s on c-018 and c-019 while many checks ran at once).
        # And a review voided by its own citations (a quote the validator could not find) judged
        # nothing either: run it once more (c-018-express-chat-app v10, 2026-10-09).
        if any("semantic review unavailable" in f or "tasks-check could not be completed" in f
               or "did not produce a valid result" in f or "invalid review citation" in f
               for f in first["findings"]):
            time.sleep(60)
            second = grade_once()
            second["retried_after"] = first["findings"][:3]
            return second
        return first

    graded = step("grade", grade_with_retry)
    critic_thread.join()
    step("shipcheck", lambda: (lambda r: (receipt_path.parent.joinpath("shipcheck.json").write_text(
        json.dumps(r, indent=1, default=str)), {"ok": r["ok"], "open": r["open"]})[1])(
        shipcheck.sweep(version, image=True, repro=True)))
    if screener is not None:
        screener.join()
        from . import genuine
        step("genuine", lambda: {k: v for k, v in genuine.audit(version, regrade=True).items()
                                 if k in ("certified", "kinds", "problems")})
    record["graded_ok"] = bool(graded.get("ok") and (graded.get("result") or {}).get("ok"))
    receipt_path.write_text(json.dumps(record, indent=1, default=str))
    return record


RELEASE = "verification/shipfix-release.json"


def calibration_record(version: Path) -> dict:
    """The calibration of record as a screen-shaped record, so the genuine audit certifies the
    campaigns that ship rather than the 5+5 screen (shipcheck.runs_certified, c-007-html-date-form
    held at packaging 2026-10-07)."""
    from .artifacts import identity
    from .benchmark.harbor import resolve_model
    version = Path(version)
    index = json.loads((version / "verification/native-calibrations.json").read_text())
    if index.get("artifact_sha256") != identity(version):
        raise ValueError("the calibration of record is bound to another version")
    per_model = {}
    for campaign in index.get("campaigns") or []:
        report = json.loads((Path(campaign) / "report.json").read_text())
        model = resolve_model(report.get("requested_model") or report["model"])
        per_model[model] = {"campaign": str(campaign), "topups": list((index.get("topups") or {}).get(str(campaign)) or [])}
    if len(per_model) < 2:
        raise ValueError(f"the calibration of record covers {sorted(per_model)} only")
    return {"per_model": per_model}


def step_rates(audit: dict) -> dict[str, dict[str, float]]:
    """Per model, per step: the share of valid episodes whose step scored 1.0."""
    seen: dict[str, dict[str, list[int]]] = {}
    for row in audit.get("episodes") or []:
        if not row.get("valid") or row.get("verdict") == "void":
            continue
        for st in row.get("steps") or []:
            hit = seen.setdefault(row["model"], {}).setdefault(st.get("name"), [0, 0])
            hit[0] += 1 if (st.get("reward") or 0) >= 1 - 1e-9 else 0
            hit[1] += 1
    return {m: {n: round(a / b, 3) for n, (a, b) in steps.items() if b} for m, steps in seen.items()}


def release(version: Path) -> dict:
    """After a clean, proven-hard final version: the calibration of record and what shipping it
    needs (user, 2026-10-07: "run 10 attempts per model per task once a task is finalised and task
    is proven to be hard"). Each step is recorded in verification/shipfix-release.json:

      revalidate  controls and probes again when their receipts are stale: policy_identity covers
                  every pipeline file, so a pipeline fix after the final run (c-043's verifier
                  pins) leaves them stale and qualification refuses (c-024, 2026-10-07)
      qualify     release.qualify, 10 episodes x 2 models; the final's reviewer, whose verdict it
                  reuses rather than asking again
      genuine     the certificate over the calibration campaigns that ship, re-graded
      hardness    pass@1 and every step under policy.STEP_PASS_RATE_MAX on both models, and a
                  usable learning signal (pipeline.learning_signal, Arena spec Oct 12)
      shipcheck   the sweep with the image probe and the from-scratch rebuild
    """
    from . import genuine, policy, release as release_mod, shipcheck
    from .author import controls, probes
    from .benchmark.harbor import resolve_model
    version = Path(version).resolve()
    path = version / RELEASE
    record = {"schema": SCHEMA + "/release", "version": str(version), "steps": {}}

    def step(name, fn):
        started = time.time()
        try:
            record["steps"][name] = {"ok": True, "result": fn()}
        except Exception as exc:                        # noqa: BLE001 -- recorded, never swallowed silently
            record["steps"][name] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:600]}"}
        record["steps"][name]["seconds"] = round(time.time() - started)
        path.write_text(json.dumps(record, indent=1, default=str))
        return record["steps"][name]

    def revalidate():
        stale = {"controls": controls.problems(version), "probes": probes.problems(version)}
        out = {"stale": {k: v[:3] for k, v in stale.items() if v}}
        if stale["controls"]:
            out["controls"] = bool(controls.run(version, workers=3).get("ok"))
        if stale["probes"] or stale["controls"]:
            out["probes"] = bool(probes.run(version).get("ok"))
        # qualify requires a harbor tasks-check receipt for this exact version. A version a harden
        # round produced never had one, so its release calibrated 10 x 2 and was then refused for the
        # receipt alone (c-001 v12, 2026-10-15). A recorded fail is kept; a missing or unverified one
        # (infrastructure) is run again.
        from .benchmark import tasks_check
        receipt = tasks_check.retained(version)
        if receipt is None or receipt.get("status") not in ("pass", "fail"):
            out["tasks_check"] = tasks_check.run(version).get("status")
        # Likewise the coverage critic: shipcheck needs its receipt for THIS version, which a harden
        # round never writes (c-001 v12, 2026-10-15). The ship rule (closed-and-verified) is what
        # `finish` runs, never a fresh search.
        from . import coverage_critic
        from .artifacts import identity
        try:
            critic = json.loads((version / coverage_critic.RECEIPT).read_text())
        except (OSError, ValueError):
            critic = None
        if not isinstance(critic, dict) or critic.get("artifact_sha256") != identity(version):
            result = coverage_critic.ship_critic(version, record_dir=version / "verification")
            out["critic"] = {"gaps": len(result["gaps"]), "mode": result.get("mode", "search")}
        return out

    step("revalidate", revalidate)
    primary, secondary = policy.resolve_calibration_models()
    reviewer = review_model(version)
    qualified = step("qualify", lambda: {k: v for k, v in release_mod.qualify(
        version, [primary, secondary], reviewer, secondary, Path(__file__).resolve().parents[1] / "pipeline-artifacts/native",
        n=policy.SPEC_EPISODES_PER_MODEL, workers=policy.SPEC_EPISODES_PER_MODEL).items() if k in ("status", "release_ready", "errors")})
    if not (qualified.get("ok") and (qualified.get("result") or {}).get("release_ready")):
        record["ready"] = False
        path.write_text(json.dumps(record, indent=1, default=str))
        return record
    audited = {}

    def certify():
        audited.update(genuine.audit(version, regrade=True, record=calibration_record(version)))
        return {k: audited.get(k) for k in ("certified", "kinds", "problems")}

    step("genuine", certify)

    def hardness():
        q = json.loads((version / "verification/qualification.json").read_text())
        metrics = (q.get("evidence") or {}).get("task_metrics") or {}
        rates = {m: r.get("pass_at_1") for m, r in metrics.items()}
        steps = step_rates(audited)
        why = [f"{m.split('/')[-1]} pass@1 {v}" for m, v in rates.items() if v is None or v >= policy.STEP_PASS_RATE_MAX]
        why += [f"{m.split('/')[-1]} step {n} {v}" for m, row in steps.items() for n, v in row.items()
                if v > policy.STEP_PASS_RATE_MAX]
        if len(rates) < 2:
            why.append(f"measured {sorted(rates)} only")
        # Arena spec Oct 12: hard is not enough -- the 10 x 2 must carry a learning signal (free points
        # bounded, several independent hard decisions, each model's rewards spread). policy.SIGNAL_*.
        from . import learning_signal
        try:
            signal = learning_signal.from_release(version)
            why += learning_signal.problems(signal)
            signal["advisories"] = learning_signal.advisories(signal)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            signal = None
            why.append(f"signal: could not be measured ({type(exc).__name__}: {str(exc)[:160]})")
        return {"pass_at_1": rates, "step_pass_rates": steps, "signal": signal,
                "valid": {m: r.get("episodes_valid") for m, r in metrics.items()}, "not_hard": why}

    hard = step("hardness", hardness)
    sweep = step("shipcheck", lambda: (lambda r: (version.joinpath("verification/shipcheck.json").write_text(
        json.dumps(r, indent=1, default=str)), {"ok": r["ok"], "open": r["open"],
        "findings": [f for f in r.get("findings") or [] if not f.get("waived")][:8]})[1])(
        shipcheck.sweep(version, image=True, repro=True)))
    record["ready"] = bool((record["steps"]["genuine"].get("result") or {}).get("certified")
                           and hard.get("ok") and not (hard.get("result") or {}).get("not_hard")
                           and sweep.get("ok") and (sweep.get("result") or {}).get("open") == 0)
    path.write_text(json.dumps(record, indent=1, default=str))
    return record


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("version", type=Path)
    ap.add_argument("--edits", help="the edits as a JSON object")
    ap.add_argument("--finish", action="store_true", help="run acceptance on the new version")
    ap.add_argument("--no-screen", action="store_true")
    ap.add_argument("--release", action="store_true",
                    help="on a final version: revalidate if stale, qualify 10 x 2, certify, sweep")
    args = ap.parse_args(argv)
    if args.release:
        record = release(args.version)
        for name, row in record["steps"].items():
            print(f"  {name:10s} {'ok' if row.get('ok') else 'FAILED'} "
                  f"{json.dumps(row.get('result') or row.get('error'), default=str)[:400]}")
        print(f"ready: {record.get('ready')}")
        return 0 if record.get("ready") else 1
    if not args.edits:
        ap.error("--edits is required unless --release")
    target = new_version(args.version, json.loads(args.edits))
    print(f"ship-fix revision: {target}")
    if args.finish:
        record = finish(target, screen=not args.no_screen)
        for name, row in record["steps"].items():
            print(f"  {name:10s} {'ok' if row.get('ok') else 'FAILED'} "
                  f"{json.dumps(row.get('result') or row.get('error'), default=str)[:220]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
