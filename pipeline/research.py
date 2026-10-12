"""Frozen factorial experiments and fresh fixed-horizon confirmation campaigns.

Plan creation is unconditional. Validation, freezing and model execution are
separate commands. Difficulty findings never reject a candidate task.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import random
import shutil
import time
import uuid
from collections import Counter
from pathlib import Path

from . import policy
from .artifacts import atomic_json, bundle_lock, freeze, identity, inventory, policy_identity
from .author.contracts import contained, strict_json
from .measurement import exact_interval, extreme_confirmation

CELLS = ("00", "10", "01", "11")  # decoy, interaction


def create_variants(source: Path, root: Path, design: dict) -> dict:
    """Create four retained drafts from declared treatments; no exports or gates."""
    source, root = Path(source).resolve(), Path(root).resolve()
    if root.is_relative_to(source):
        raise ValueError("factorial drafts need a fresh destination outside their source")
    root.mkdir(parents=True, exist_ok=False)
    base = strict_json((source / "authoring/spec.json").read_text())
    variants = {}
    for cell in CELLS:
        bundle = root / cell
        shutil.copytree(source, bundle, ignore=shutil.ignore_patterns("verification", "hardened", "__pycache__", "*.pyc"))
        spec = copy.deepcopy(base)
        spec.update(copy.deepcopy(design.get("spec_overrides", {}).get(cell, {})))
        if cell[0] == "1":
            path = bundle / "authoring/extraction/fixture-authored.json"
            authored = strict_json(path.read_text()) if path.exists() else {"files": {}}
            for rel, content in design.get("decoy_files", {}).items():
                target = contained(bundle / "environment/repo", rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
                authored.setdefault("files", {})[rel] = {"reason": design.get("decoy", "declared research decoy treatment"),
                    "sha256": hashlib.sha256(content.encode()).hexdigest(), "origin": "authored"}
            atomic_json(path, authored)
        if cell[1] == "1":
            spec.setdefault("suites", {}).update(copy.deepcopy(design.get("interaction_suites", {})))
        spec["_research_variant"] = {"cell": cell, "parent_spec_sha256": digest(base), "treatment": design.get("treatment", {})}
        atomic_json(bundle / "authoring/spec.json", spec)
        atomic_json(bundle / "verification/variant-creation.json", {"phase": "creation", "validation_deferred": True})
        variants[cell] = str(bundle)
    atomic_json(root / "design.json", design)
    return {"phase": "creation", "validation_deferred": True, "variants": variants}


def export_candidate(bundle: Path) -> None:
    """Generated variants are first exported and controlled during validation."""
    lineage_marker = bundle / "verification/lineage-validation.json"
    if lineage_marker.exists() and strict_json(lineage_marker.read_text()).get("validation_deferred"):
        from .harden import attempt
        origin = strict_json(lineage_marker.read_text())["origin"]
        source = Path(origin["parent"])
        if identity(source) != origin["parent_artifact_sha256"]:
            raise ValueError("checkpoint lineage parent changed since creation")
        findings = attempt(source, bundle, [], [], kind="extend",
                           proposed=strict_json((bundle / "authoring/spec.json").read_text()))
        atomic_json(lineage_marker, {"phase": "validation", "validation_deferred": bool(findings),
                    "verdict": "blocked" if findings else "green", "findings": findings, "origin": origin})
        if findings:
            raise ValueError("; ".join(findings))
    marker = bundle / "verification/variant-creation.json"
    if not marker.exists() or not strict_json(marker.read_text()).get("validation_deferred"):
        return
    from .author.build import build
    from .author.validate import validate
    from .author.controls import run as controls
    from .author.probes import run as probes
    from .repair import rebuild_ledger
    spec = strict_json((bundle / "authoring/spec.json").read_text())
    issues = validate(spec, bundle)
    if issues:
        raise ValueError("; ".join(issues))
    previous = strict_json((bundle / "provenance/ledger.json").read_text())
    build(spec, bundle)
    rebuild_ledger(bundle, previous)
    receipt = controls(bundle)
    probes(bundle, controls_receipt=receipt)
    if receipt.get("errors") or not receipt.get("ok"):
        raise ValueError(f"variant executed controls: {receipt.get('errors')}")
    atomic_json(marker, {"phase": "validation", "validation_deferred": False, "verdict": "exported"})


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def zero_success_horizon(models: int, target: float) -> int:
    return math.ceil(math.log(.05 / models) / math.log1p(-target))


def confirmation_registry() -> Path:
    from .config import ARTIFACTS
    return ARTIFACTS / "research" / "confirmation-registry.json"


def confirmation_key(manifest: dict) -> str:
    return digest({"artifact": next(iter(manifest["variants"].values()))["artifact_sha256"],
                   "models": sorted(manifest["models"]), "protocol": manifest["protocol"],
                   "policy_sha256": manifest["policy_sha256"]})


def reserve_confirmation(root: Path, manifest: dict) -> None:
    """One confirmation cohort per candidate/protocol; no retrying favorable scores."""
    path = confirmation_registry()
    with bundle_lock(path.parent):
        registry = strict_json(path.read_text()) if path.exists() else {}
        key = confirmation_key(manifest)
        prior = registry.get(key)
        if prior and prior["root"] != str(root):
            raise ValueError(f"this candidate/protocol already has a confirmation plan at {prior['root']}; "
                             "resume and report that plan, do not select a new favorable cohort")
        registry[key] = {"root": str(root), "manifest_sha256": digest(manifest)}
        atomic_json(path, registry)


def create_plan(root: Path, variants: dict, *, purpose: str = "confirmation", episodes: int | None = None,
                models: list[str] | None = None, treatment: dict | None = None, seed: int = 0) -> dict:
    """Retain an editable plan even if its candidates or experiment are incomplete."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    models = list(models or policy.resolve_calibration_models())
    plan = {"schema": "arena-coding/research-plan/1", "id": uuid.uuid4().hex,
            "purpose": purpose, "created_at": time.time(), "phase": "creation",
            "validation_deferred": True, "variants": {k: str(Path(v).resolve()) for k, v in variants.items()},
            "models": models, "episodes": episodes if episodes is not None else
            zero_success_horizon(max(1, len(models)), policy.EXTREME_TARGET_PASS_AT_1),
            "target": policy.EXTREME_TARGET_PASS_AT_1, "treatment": treatment or {}, "seed": seed}
    atomic_json(root / "plan.json", plan)
    return plan


def contrast_problems(bundles: dict[str, Path]) -> list[str]:
    """Factorial contrasts preserve the same public contract and scoring schema."""
    errors = []
    inv = {k: inventory(v) for k, v in bundles.items()}
    def select(cell, predicate):
        return {p: h for p, h in inv[cell].items() if predicate(p)}
    baseline = bundles["00"]
    instructions = select("00", lambda p: p.startswith("steps/") and p.endswith("/instruction.md"))
    checks = (baseline / "tests/checks.json").read_bytes()
    for cell in CELLS:
        if select(cell, lambda p: p.startswith("steps/") and p.endswith("/instruction.md")) != instructions:
            errors.append(f"{cell}: factorial variants must publish identical instructions")
        if (bundles[cell] / "tests/checks.json").read_bytes() != checks:
            errors.append(f"{cell}: factorial variants must retain identical check schema and weights")
        if select(cell, lambda p: p.startswith("environment/") and not p.startswith("environment/repo/")) != select(
                "00", lambda p: p.startswith("environment/") and not p.startswith("environment/repo/")):
            errors.append(f"{cell}: toolchain or agent image changed between contrasts")
    for a, b in (("00", "10"), ("01", "11")):
        if select(a, lambda p: p.startswith("tests/")) != select(b, lambda p: p.startswith("tests/")):
            errors.append(f"{a}/{b}: decoy-only contrast changed hidden tests")
        if select(a, lambda p: p.startswith("environment/repo/")) == select(b, lambda p: p.startswith("environment/repo/")):
            errors.append(f"{a}/{b}: decoy contrast has no public treatment")
    for a, b in (("00", "01"), ("10", "11")):
        if select(a, lambda p: p.startswith("environment/repo/")) != select(b, lambda p: p.startswith("environment/repo/")):
            errors.append(f"{a}/{b}: interaction-only contrast changed public fixture")
        if select(a, lambda p: p.startswith("tests/suites/")) == select(b, lambda p: p.startswith("tests/suites/")):
            errors.append(f"{a}/{b}: interaction contrast has no hidden-input treatment")
    return errors


def validate_plan(root: Path) -> dict:
    """Authoritative validation happens after plan creation, before any spending."""
    from .benchmark.harbor import resolve_model
    from .benchmark.preflight import validate_task
    from .run import run_bundle
    root = Path(root).resolve()
    with bundle_lock(root):
        if (root / "manifest.json").exists():
            return sealed(root)
        plan = strict_json((root / "plan.json").read_text())
        problems = []
        models = [resolve_model(m) for m in plan.get("models") or []]
        if len(set(models)) < 2 or len(set(models)) != len(models):
            problems.append("research needs at least two distinct named models")
        problems += policy.calibration_problems(plan.get("models") or [])
        if plan.get("purpose") not in ("confirmation", "ablation"):
            problems.append("purpose must be confirmation or ablation")
        if type(plan.get("episodes")) is not int or plan["episodes"] < 1:
            problems.append("predeclare a positive fixed episode horizon")
        if type(plan.get("target")) not in (int, float) or not 0 < plan["target"] <= policy.EXTREME_TARGET_PASS_AT_1:
            problems.append("confirmation target must be at least as strict as the extreme policy target")
        paths = {k: Path(v) for k, v in plan.get("variants", {}).items()}
        if plan.get("purpose") == "confirmation" and len(paths) != 1:
            problems.append("confirmation freezes exactly one selected final candidate")
        if plan.get("purpose") == "ablation":
            if set(paths) != set(CELLS):
                problems.append("ablation needs 00, 10, 01 and 11 variants")
            if not all(isinstance(plan.get("treatment", {}).get(k), str) and plan["treatment"][k].strip()
                       for k in ("decoy", "interaction", "public_disambiguation")):
                problems.append("describe both treatments and the public evidence that resolves the decoy")
        fingerprints, validated_evidence = {}, {}
        for cell, bundle in paths.items():
            try:
                if not cell or not cell.replace("-", "").replace("_", "").isalnum():
                    raise ValueError("variant names must be safe directory names")
                with bundle_lock(bundle):
                    export_candidate(bundle)
                    validate_task(bundle)
                    gates = run_bundle(bundle, Path.cwd())
                    if gates["verdict"] != "green":
                        raise ValueError(f"validity gate chain is {gates['verdict']}")
                    fingerprints[cell] = identity(bundle)
                    validated_evidence[cell] = {f"verification/{name}": hashlib.sha256(p.read_bytes()).hexdigest()
                        for name in ("controls.json", "probes.json", "semantic-review.json")
                        if (p := bundle / "verification" / name).is_file() and not p.is_symlink()}
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                problems.append(f"{cell}: {exc}")
        if plan.get("purpose") == "ablation" and set(paths) == set(CELLS) and len(fingerprints) == 4:
            problems += contrast_problems(paths)
        receipt = {"phase": "validation", "verdict": "blocked" if problems else "green", "problems": problems}
        atomic_json(root / "validation.json", receipt)
        if problems:
            return receipt
        variants = {}
        freeze_id = uuid.uuid4().hex  # partial failed freezes can never prevent a validation retry
        for cell, bundle in paths.items():
            snapshot = root / "variants" / freeze_id / cell
            with bundle_lock(bundle):
                sha = freeze(bundle, snapshot)
                if sha != fingerprints[cell]:
                    raise ValueError("candidate changed after validation")
                # Controls/review are private evidence used for failure attribution.
                evidence = {}
                for name in ("controls.json", "probes.json", "semantic-review.json"):
                    p = bundle / "verification" / name
                    if p.is_file() and not p.is_symlink():
                        dest = snapshot / "verification" / p.name
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(p, dest)
                        evidence[f"verification/{name}"] = hashlib.sha256(dest.read_bytes()).hexdigest()
                if evidence != validated_evidence[cell]:
                    raise ValueError("control or review evidence changed after validation")
            variants[cell] = {"bundle": str(snapshot), "artifact_sha256": sha, "validation_evidence": evidence}
        jobs = [{"id": uuid.uuid4().hex, "variant": cell, "model": model}
                for cell in variants for model in plan["models"]]
        random.Random(plan["seed"]).shuffle(jobs)
        manifest = {**plan, "phase": "validation", "validation_deferred": False, "variants": variants,
                    "models": models, "jobs": jobs, "frozen_at": time.time(),
                    "policy_sha256": policy_identity(), "root": str(root),
                    "protocol": {"effort": policy.SPEC_CALIBRATION_EFFORT, "no_limits": policy.SPEC_NO_LIMITS,
                                 "harness": {m: policy.harness_for(m) for m in models}},
                    "validity": receipt}
        if manifest["purpose"] == "confirmation":
            try:
                reserve_confirmation(root, manifest)
            except ValueError as exc:
                receipt = {"phase": "validation", "verdict": "blocked", "problems": [str(exc)]}
                atomic_json(root / "validation.json", receipt)
                return receipt
        atomic_json(root / "manifest.json", manifest)
        atomic_json(root / "seal.json", {"manifest_sha256": digest(manifest)})
        return manifest


def sealed(root: Path) -> dict:
    manifest = strict_json((root / "manifest.json").read_text())
    if digest(manifest) != strict_json((root / "seal.json").read_text())["manifest_sha256"]:
        raise ValueError("research manifest changed after freezing")
    if manifest["root"] != str(root.resolve()) or manifest["policy_sha256"] != policy_identity():
        raise ValueError("research root or pipeline protocol changed; preserve this plan as historical evidence")
    if manifest["purpose"] == "confirmation":
        registry = strict_json(confirmation_registry().read_text())
        if registry.get(confirmation_key(manifest)) != {"root": str(root.resolve()), "manifest_sha256": digest(manifest)}:
            raise ValueError("confirmation registry does not authorize this frozen cohort")
    for row in manifest["variants"].values():
        bundle = Path(row["bundle"])
        if not bundle.resolve().is_relative_to(root.resolve() / "variants") or identity(bundle) != row["artifact_sha256"]:
            raise ValueError("frozen research variant changed")
        for rel, expected in row.get("validation_evidence", {}).items():
            path = contained(bundle, rel)
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("frozen validation evidence changed")
    return manifest


def run(root: Path, *, workers: int = 4) -> dict:
    """Run fresh native cohorts; resume scores, and replace voids only."""
    from .release import _calibrate_model
    root = Path(root).resolve()
    with bundle_lock(root):
        manifest = sealed(root)
        state_path = root / "state.json"
        state = strict_json(state_path.read_text()) if state_path.exists() else {"jobs": {}}
        for job in manifest["jobs"]:
            row = state["jobs"].setdefault(job["id"], {})
            if row.get("complete"):
                continue
            job_root = root / "runs" / job["id"]
            # A crash can occur after scoring but before recording the returned paths.
            # Discover every reserved output, never only the favorable saved subset.
            try:
                existing = campaign_parts(job_root)
                if any(str(Path(p).resolve()) not in {str(Path(q).resolve()) for q in existing} for p in row.get("parts", [])):
                    raise ValueError("retained campaign is missing from its reserved job directory")
                _fresh_parts(root, manifest, job, existing)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                row.update(error=f"retained campaign unverified: {exc}", complete=False)
                atomic_json(state_path, state)
                continue
            row.update(started_at=row.get("started_at", time.time()), phase="measurement", parts=existing)
            atomic_json(state_path, state)  # reserve this job before launching anything
            bundle = Path(manifest["variants"][job["variant"]]["bundle"])
            try:
                primary, topups, ok = _calibrate_model(bundle, bundle, job_root, job["model"],
                    manifest["episodes"], workers, "research-topup", existing=existing or None)
                row.update(parts=[primary, *topups], complete=ok, error=None)
            except Exception as exc:
                row.update(error=f"{type(exc).__name__}: {exc}", complete=False)
            atomic_json(state_path, state)
        return report(root)


def campaign_parts(job_root: Path) -> list[str]:
    paths = list(job_root.glob("calibration-*"))
    if any(p.is_symlink() or not p.is_dir() or (p / "report.json").is_symlink() for p in paths):
        raise ValueError("reserved campaigns must be real directories with regular reports")
    return [str(p) for p in sorted(paths, key=lambda p: (
        strict_json((p / "report.json").read_text()).get("started_at", 0), p.name))]


def _fresh_parts(root: Path, manifest: dict, job: dict, parts: list) -> None:
    expected = root / "runs" / job["id"]
    if expected.is_symlink() or not expected.resolve().is_relative_to(root.resolve() / "runs"):
        raise ValueError("research job directory escapes its reserved campaign root")
    expected = expected.resolve()
    for part in parts:
        path = Path(part)
        if path.is_symlink() or (path / "report.json").is_symlink() or not path.resolve().is_relative_to(expected):
            raise ValueError("foreign or reused campaign cannot enter this research cohort")
        native = strict_json((path / "report.json").read_text())
        if native.get("started_at", 0) < manifest["frozen_at"]:
            raise ValueError("campaign predates the predeclared frozen plan")
        from .benchmark.harbor import resolve_model
        if resolve_model(native.get("requested_model") or native.get("model")) != resolve_model(job["model"]):
            raise ValueError("campaign is assigned to a different predeclared model")
    physical = {str(Path(p).resolve()) for p in campaign_parts(expected)}
    if physical != {str(Path(p).resolve()) for p in parts}:
        raise ValueError("research cohort omitted a reserved campaign; scored episodes cannot be discarded")


def factorial(records: dict, models: list[str]) -> dict:
    """Simultaneous conservative intervals for the three factorial contrasts."""
    result = {}
    for model in models:
        rows = {c: records[c]["per_model"].get(model, {}) for c in CELLS}
        intervals = {c: exact_interval(r.get("passes", 0), r.get("episodes_valid", 0),
                                      alpha=.05 / (4 * len(models))) for c, r in rows.items()}
        contrasts = {}
        for name, weights in (("decoy", {"10": .5, "11": .5, "00": -.5, "01": -.5}),
                              ("interaction", {"01": .5, "11": .5, "00": -.5, "10": -.5}),
                              ("joint_excess", {"11": 1., "10": -1., "01": -1., "00": 1.})):
            ready = all(intervals[c] is not None for c in CELLS)
            bounds = [sum(w * intervals[c][0 if w > 0 else 1] for c, w in weights.items()),
                      sum(w * intervals[c][1 if w > 0 else 0] for c, w in weights.items())] if ready else None
            estimate = sum(w * rows[c]["pass_at_1"] for c, w in weights.items()) if ready else None
            contrasts[name] = {"success_rate_change": estimate, "simultaneous_95_interval": bounds,
                               "status": "reduces_success" if bounds and bounds[1] < 0 else
                                         "increases_success" if bounds and bounds[0] > 0 else "inconclusive"}
        result[model] = contrasts
    return {"by_model": result, "note": "negative means harder; treatment causality also requires valid matched contrasts and fresh independent attempts"}


def failure_patterns(bundle: Path, index: dict) -> dict:
    """Joint failure signatures, not an assumption that checks fail independently."""
    from .release import calibration_measurement
    from . import attribution
    signatures = Counter()
    for primary in index.get("campaigns", []):
        problems, measured = calibration_measurement(bundle, index, primary, 1)
        if problems:
            continue
        verdicts = attribution.attribute(bundle, measured).get("episodes", {})
        grouped = {}
        for row in measured.get("rollouts", []):
            grouped.setdefault(row["episode"], []).append(row)
        for episode, rows in grouped.items():
            if (verdicts.get(str(episode), verdicts.get(episode)) or {}).get("verdict") not in ("capability", "contract", "pass"):
                continue
            failed = tuple(sorted(f"{r['step']}/{c}" for r in rows for c, value in r.get("checks", {}).items() if value == "failed"))
            signatures[(measured.get("model"), failed)] += 1
    return {"patterns": [{"model": m, "failed": list(s), "episodes": n} for (m, s), n in signatures.items()],
            "note": "a shared failure signature is consistency evidence, not proof of hypothesis adoption"}


def report(root: Path) -> dict:
    from .screen import summarize
    root = Path(root).resolve()
    manifest = sealed(root)
    state_path = root / "state.json"
    state = strict_json(state_path.read_text()) if state_path.exists() else {"jobs": {}}
    records, indices, problems, seen = {}, {}, [], set()
    for cell, variant in manifest["variants"].items():
        index = {"artifact_sha256": variant["artifact_sha256"], "campaigns": [], "topups": {}}
        for job in manifest["jobs"]:
            if job["variant"] != cell:
                continue
            parts = state.get("jobs", {}).get(job["id"], {}).get("parts") or []
            try:
                if not parts:
                    raise ValueError("predeclared model cohort has not run")
                _fresh_parts(root, manifest, job, parts)
                if any(str(Path(p).resolve()) in seen for p in parts):
                    raise ValueError("a campaign occurs in more than one cohort")
                seen.update(str(Path(p).resolve()) for p in parts)
                if parts:
                    index["campaigns"].append(parts[0])
                    index["topups"][parts[0]] = parts[1:]
            except (OSError, ValueError, TypeError, KeyError) as exc:
                problems.append(f"{cell}/{job['model']}: {exc}")
        indices[cell] = index
        records[cell] = summarize(Path(variant["bundle"]), index, manifest["episodes"], manifest["models"])
        if not records[cell]["measurement_complete"]:
            problems += [f"{cell}: {p}" for p in records[cell]["shippable_problems"]]
        if any(row["episodes_valid"] != manifest["episodes"] for row in records[cell]["per_model"].values()):
            problems.append(f"{cell}: cohort differs from its predeclared fixed horizon")
    result = {"schema": "arena-coding/research-result/1", "manifest_sha256": digest(manifest),
              "purpose": manifest["purpose"], "measurement_complete": not problems, "problems": problems,
              "variants": records, "confirmed_extreme": False, "difficulty_is_advisory": True}
    result["claim_scope"] = "this frozen task and protocol; per-candidate confidence, not a simultaneous guarantee over adaptive task-family searches"
    if manifest["purpose"] == "confirmation":
        record = next(iter(records.values()))
        confirmation = extreme_confirmation(record["per_model"], manifest["target"], held_out=not problems)
        result.update(confirmation=confirmation, confirmed_extreme=confirmation["confirmed"])
    elif not problems:
        result["factorial"] = factorial(records, manifest["models"])
    result["failure_patterns"] = {c: failure_patterns(Path(v["bundle"]), indices[c]) for c, v in manifest["variants"].items()}
    result["adoption"] = adoption_report(root, manifest, indices)
    atomic_json(root / "result.json", result)
    return result


def record_adoption(root: Path, claims: list[dict]) -> dict:
    """Keep analyst claims; their citations and episode identities are rechecked on every read."""
    root = Path(root).resolve()
    sealed(root)
    atomic_json(root / "adoption.json", claims)
    return report(root)


def adoption_report(root: Path, manifest: dict, indices: dict) -> dict:
    from .release import calibration_measurement
    from .artifacts import evidence_inventory
    path = root / "adoption.json"
    claims = strict_json(path.read_text()) if path.exists() else []
    accepted, unresolved, used = [], [], set()
    campaigns = {p for index in indices.values() for primary in index["campaigns"]
                 for p in [primary, *index["topups"].get(primary, [])]}
    for claim in claims:
        try:
            cell, campaign = claim["variant"], claim["campaign"]
            if campaign not in campaigns or cell not in indices:
                raise ValueError("claim cites a campaign outside this frozen experiment")
            bundle = Path(manifest["variants"][cell]["bundle"])
            if campaign not in {p for primary in indices[cell]["campaigns"] for p in [primary, *indices[cell]["topups"].get(primary, [])]}:
                raise ValueError("campaign belongs to another variant")
            issues, measured = calibration_measurement(bundle, {"topups": {}}, campaign, 1)
            if issues:
                raise ValueError("claim has invalid campaign evidence")
            from . import attribution
            trial = next(t for t in measured.get("trials", []) if t.get("id") == claim["trial"])
            if not trial.get("valid") or not claim.get("reason"):
                raise ValueError("claim needs a valid trial and an explanation")
            kinds = attribution.attribute(bundle, measured).get("episodes", {})
            episode = measured["trials"].index(trial) + 1
            if (kinds.get(str(episode), kinds.get(episode)) or {}).get("verdict") not in ("capability", "contract", "pass"):
                raise ValueError("unadjudicated or non-capability failure cannot establish adoption")
            spec = strict_json((bundle / "authoring/spec.json").read_text())
            hypotheses = spec.get("quality", {}).get("difficulty_case", {}).get("hypotheses", [])
            if claim["hypothesis"] not in {h["id"] for h in hypotheses} or claim["verdict"] not in ("adopted", "other", "unresolved"):
                raise ValueError("unknown hypothesis or adoption verdict")
            citations = claim.get("citations") or []
            if not citations:
                raise ValueError("cite solver behavior, not just a failed check")
            native = strict_json((Path(campaign) / "report.json").read_text())
            hashes = native.get("evidence_files") or {}
            for citation in citations:
                rel = citation["file"]
                text = contained(Path(campaign), rel).read_text()
                if (rel not in hashes or evidence_inventory(Path(campaign), [rel]).get(rel) != hashes[rel]
                        or not citation.get("quote") or citation["quote"] not in text):
                    raise ValueError("adoption citation is missing, stale or not verbatim")
                if trial["id"] not in rel.split("/") or "/agent/" not in rel:
                    raise ValueError("cite this trial's agent transcript or produced patch, not hidden grader material")
            key = (cell, campaign, trial["id"], claim["hypothesis"])
            if key in used:
                raise ValueError("duplicate adoption claim")
            used.add(key)
            accepted.append({**claim, "model": measured["model"]})
        except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
            unresolved.append({"claim": claim, "problem": str(exc)})
    counts = Counter((c["variant"], c["model"], c["hypothesis"], c["verdict"]) for c in accepted)
    coverage = []
    from .metrics import task_metrics
    for cell, index in indices.items():
        bundle = Path(manifest["variants"][cell]["bundle"])
        spec = strict_json((bundle / "authoring/spec.json").read_text())
        hypotheses = spec.get("quality", {}).get("difficulty_case", {}).get("hypotheses", [])
        for primary in index["campaigns"]:
            issues, measured = calibration_measurement(bundle, index, primary, 1)
            if issues:
                continue
            model, n = measured.get("model"), task_metrics(measured)["episodes_valid"]
            for h in hypotheses:
                hid = h["id"]
                adopted, other = counts[cell, model, hid, "adopted"], counts[cell, model, hid, "other"]
                unknown = max(0, n - adopted - other)
                coverage.append({"variant": cell, "model": model, "hypothesis": hid, "valid_episodes": n,
                    "adopted": adopted, "other": other, "unadjudicated": unknown,
                    "logical_rate_bounds": [adopted / n, (adopted + unknown) / n] if n else None})
    return {"claims": accepted, "unresolved": unresolved,
            "coverage": coverage,
            "counts": [{"variant": v, "model": m, "hypothesis": h, "verdict": status, "episodes": n}
                       for (v, m, h, status), n in counts.items()],
            "note": "adoption requires cited solver behavior and analyst adjudication; unknown episodes are not negative evidence; "
                    "logical bounds are not statistical confidence intervals; failed-check matches alone are unresolved"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    variants = sub.add_parser("variants")
    variants.add_argument("source")
    variants.add_argument("--out", required=True)
    variants.add_argument("--design", required=True, help="JSON with decoy_files, interaction_suites and optional per-cell spec_overrides")
    create = sub.add_parser("prepare")
    create.add_argument("root")
    create.add_argument("--purpose", choices=("confirmation", "ablation"), default="confirmation")
    create.add_argument("--variant", action="append", default=[], help="name=path; ablation uses 00,10,01,11")
    create.add_argument("--episodes", type=int)
    create.add_argument("--models", nargs="+")
    create.add_argument("--treatment", help="JSON file describing decoy, interaction and public_disambiguation")
    create.add_argument("--seed", type=int, default=0)
    for command in ("validate", "run", "report", "adjudicate"):
        parser = sub.add_parser(command)
        parser.add_argument("root")
        if command == "run":
            parser.add_argument("--workers", type=int, default=4)
        if command == "adjudicate":
            parser.add_argument("--claims", required=True)
    args = ap.parse_args(argv)
    if args.command == "variants":
        print(json.dumps(create_variants(Path(args.source), Path(args.out), strict_json(Path(args.design).read_text())), indent=2))
        return 0
    root = Path(args.root)
    if args.command == "prepare":
        variants = dict(v.split("=", 1) for v in args.variant)
        result = create_plan(root, variants, purpose=args.purpose, episodes=args.episodes, models=args.models,
                             treatment=strict_json(Path(args.treatment).read_text()) if args.treatment else None, seed=args.seed)
    else:
        from .cli import load_keys
        load_keys(prompt_if_missing=False)
        result = (validate_plan(root) if args.command == "validate" else run(root, workers=args.workers) if args.command == "run"
                  else record_adoption(root, strict_json(Path(args.claims).read_text())) if args.command == "adjudicate" else report(root))
    print(json.dumps(result, indent=2))
    return 1 if result.get("verdict") == "blocked" or result.get("problems") else 0


if __name__ == "__main__":
    raise SystemExit(main())
