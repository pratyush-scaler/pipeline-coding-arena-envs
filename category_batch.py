"""Quota-driven entry point for the Arena pipeline.

Run ``python category_batch.py --out envs-cat-v2 --per-category 10``.
The controller freezes the pipeline in its own artifact workspace, preserves
every unsuccessful candidate there, and publishes only qualified tasks. It
does not change the policy underneath another active build.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

# The categories are Arena's faithfulness levels, as TARGETS classified before
# authoring (user, 2026-09-26): one quota and one folder per level.
from pipeline.policy import TARGET_LEVEL_DIRS as CATEGORIES  # noqa: E402
QUALIFICATION_STAGES = {"validated", "qualification-queued", "qualifying", "qualifying-external", "qualified-reserve"}
AUTHOR_RESUME_STAGES = {"resume-session", "resume-build", "validity-queued"}
VALIDITY_STAGES = {"awaiting-hardening", "unverified", "blocked", "qualification-rejected", "validity-blocked"}


from pipeline import config as _config
from pipeline import policy as _policy


def validity_authorization(request, key):
    """An explicit batch instruction authorizes validity work, never hardening."""
    value = request.get('validity_revision')
    if not value or key not in value.get('targets', []):
        return None
    if (not value.get('id') or not value.get('instruction')
            or value.get('purpose') != 'validity-before-calibration'
            or type(value.get('max_passes')) is not int or not 1 <= value['max_passes'] <= 3
            or type(value.get('max_steps')) is not int or not 1 <= value['max_steps'] <= 140):
        raise ValueError('Invalid explicit validity-revision authorization')
    return value


def validity_findings(record):
    """Use task defects, not measured scores or individual solver answers."""
    build = record.get('build', {})
    if build.get('status') == 'validated':
        # Earlier rejection notes can survive a successful revision. Only a
        # current, artifact-bound semantic rejection can reopen a validated
        # task; calibration failures must not reactivate historical findings.
        candidate = record.get('candidate')
        return review_validity_findings(Path(candidate)) if candidate else []
    findings = list(record.get('validity_findings', []))
    if build.get('stage_reached') in {'gate', 'semantic-review', 'authoring-progress'}:
        findings += build.get('repair_feedback') or build.get('blockers', [])
    return list(dict.fromkeys(str(item) for item in findings if item and not any(
        token in str(item).lower() for token in (
            'calibration:', 'measured bands', 'measured core', 'pilot selection:',
            'semantic review could not be verified:', 'provider request or wall-clock',
            'provider request budget exhausted'))))


def validity_passes_left(candidate, authorization):
    from pipeline.checkpoint import read as checkpoint
    progress = checkpoint(candidate).get('validity_revision', {})
    used = progress.get('passes_started', 0) if progress.get('authorization_id') == authorization['id'] else 0
    if type(used) is not int or used < 0:
        raise ValueError('Invalid retained validity revision count')
    return max(0, authorization['max_passes'] - used)


def prepare_validity_revision(candidate, authorization, findings):
    """Archive and charge a new authorized pass without resetting older budgets."""
    from pipeline import checkpoint as progress
    from pipeline.artifacts import atomic_json, bundle_lock, evidence_inventory, freeze, ROOTS
    from pipeline.author.contracts import contained
    candidate = Path(candidate).resolve()
    with bundle_lock(candidate):
        state = progress.read(candidate)
        previous = state.get('validity_revision', {})
        same = previous.get('authorization_id') == authorization['id']
        if same and previous.get('active'):
            if not progress.resumable(candidate):
                raise ValueError('Authorized validity pass has no safe continuation')
            return state
        if not findings or not validity_passes_left(candidate, authorization):
            raise ValueError('No actionable validity findings or authorized passes remain')
        author = state.get('authoring_session')
        if author:
            if (evidence_inventory(candidate, [author['session']]) != author['session_files']
                    or evidence_inventory(candidate, progress.WORKSPACE) != author['workspace_files']):
                raise ValueError('Retained draft changed after its saved author boundary')
            draft = contained(candidate, author['session']) / 'draft.json'
            spec = read(draft) if draft.exists() else {}
        elif state.get('submitted') and state.get('stage') in {'submitted', 'checking', 'repair'}:
            spec = progress.submitted_spec(candidate, state)
        else:
            spec = read(candidate / 'authoring/spec.json')
        number = (previous.get('passes_started', 0) if same else 0) + 1
        archive = contained(candidate, f'verification/validity-revisions/{authorization["id"]}/pass-{number:02d}')
        if archive.exists():
            raise ValueError('Validity archive already exists; inspect interrupted preparation')
        archive.mkdir(parents=True)
        atomic_json(archive / 'checkpoint-before.json', state)
        if (candidate / 'task.toml').exists() and (candidate / 'tests/checks.json').exists():
            freeze(candidate, archive / 'task')
        else:
            # A stalled first draft still has recoverable, hash-checked sources.
            files = evidence_inventory(candidate, ROOTS)
            for relative in files:
                target = contained(archive / 'task', relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(contained(candidate, relative), target)
        atomic_json(archive / 'authorization.json', authorization)
        atomic_json(archive / 'findings.json', findings)
        for name in ('state.json', 'semantic-review.json', 'qualification.json', 'release.json'):
            source = candidate / 'verification' / name
            if source.exists():
                shutil.copy2(source, archive / name)
        progress.submitted(candidate, archive, spec)
        return progress.update(candidate, stage='repair', phase=None, feedback=[
            'The user authorized validity correction before calibration. Fix all applicable source, instruction, '
            'grader, privacy and professional-coverage defects in this bounded pass. Preserve substantive '
            'difficulty and the two-step assignment. Difficulty hardening based on measured scores is deferred '
            'until the user reviews calibration. Do not tune against individual solver answers.', *findings],
            validity_revision={'authorization_id': authorization['id'], 'passes_started': number,
                'active': True, 'archive': archive.relative_to(candidate).as_posix()})


def review_validity_findings(candidate):
    from pipeline.artifacts import identity
    from pipeline.author.contracts import contained
    from pipeline.review import review_problems, semantic_rejection
    try:
        report = read(candidate / 'verification/semantic-review.json')
        if report.get('artifact_sha256') != identity(candidate):
            return []
        issues = review_problems(candidate, report)
        if not semantic_rejection(issues):
            return []
        doc = read(contained(candidate, report['work_dir']) / 'deliverables/review.json')
        findings = [f"Semantic review {check.get('id')}: {check.get('reason')}"
                    for check in doc.get('checks', []) if check.get('verdict') != 'pass']
        for name in ('ambiguities', 'substantive_gaps', 'shortcut_risks', 'privacy_findings'):
            findings.extend(f'Semantic review {name}: {value}' for value in doc.get(name, []))
        return findings
    except (OSError, ValueError, KeyError, TypeError):
        return []


def completed_calibration(candidate, models):
    """Record valid measurement separately from passing the difficulty rubric."""
    from pipeline.artifacts import identity
    from pipeline.release import calibration_measurement
    from pipeline.benchmark.harbor import resolve_model
    from pipeline.measurement import task_profile
    try:
        index = read(candidate / 'verification/native-calibrations.json')
        if index.get('artifact_sha256') != identity(candidate):
            return None
        reports = {}
        for path in index.get('campaigns', []):
            # Top-up campaigns recorded under `topups` replace voids in this
            # campaign; reading the primary path alone would miss them.
            issues, measured = calibration_measurement(candidate, index, path, 10)
            if issues:
                return None
            profile = task_profile(measured['by_step'])
            reports[measured['model']] = {'campaign': path,
                'summary': read(Path(path) / 'report.json')['summary'],
                'difficulty': {key: profile[key] for key in ('status', 'bands', 'uninformative_steps')},
                'by_step': measured['by_step']}
        if not {resolve_model(model) for model in models}.issubset(reports):
            return None
        return {'artifact_sha256': index['artifact_sha256'], 'models': reports, 'complete': True}
    except (OSError, ValueError, KeyError, TypeError):
        return None


class ProviderAdmission:
    """FIFO admission per provider with one shared request budget.

    No wall-clock deadline: calibration runs with no time limits, and a deadline
    here turned every rollout after it into a void. Elapsed time is reported,
    never enforced. A spent request budget raises `ProviderBudgetExhausted`,
    which stops the batch instead of voiding the episodes in flight.
    """
    def __init__(self, limits, max_requests=20000,
                 spent_requests=0, elapsed_seconds=0):
        if (not limits or any(type(n) is not int or n < 1 for n in limits.values())
                or max_requests < 1
                or spent_requests < 0 or elapsed_seconds < 0):
            raise ValueError("Invalid provider admission budget")
        self.limits = dict(limits)
        self.condition = threading.Condition()
        self.queues = {name: deque() for name in limits}
        self.active = Counter()
        self.admitted = Counter()
        self.wait_seconds = Counter()
        self.max_wait_seconds = Counter()
        self.requests = spent_requests
        self.max_requests = max_requests
        self.started = time.monotonic() - elapsed_seconds

    @contextmanager
    def slot(self, provider="anthropic"):
        ticket, queued_at = object(), time.monotonic()
        with self.condition:
            queue = self.queues[provider]
            queue.append(ticket)
            self.condition.notify_all()
            try:
                while True:
                    if self.requests >= self.max_requests:
                        from pipeline.provider import EXHAUSTED, ProviderBudgetExhausted
                        raise ProviderBudgetExhausted(EXHAUSTED)
                    if queue[0] is ticket and self.active[provider] < self.limits[provider]:
                        break
                    self.condition.wait()
                queue.popleft()
                self.requests += 1
                self.admitted[provider] += 1
                self.active[provider] += 1
                waited = time.monotonic() - queued_at
                self.wait_seconds[provider] += waited
                self.max_wait_seconds[provider] = max(self.max_wait_seconds[provider], waited)
            except BaseException:
                if ticket in queue:
                    queue.remove(ticket)
                raise
            finally:
                self.condition.notify_all()
        try:
            yield
        finally:
            with self.condition:
                self.active[provider] -= 1
                self.condition.notify_all()

    def stats(self):
        with self.condition:
            return {"requests": self.requests, "max_requests": self.max_requests,
                "elapsed_seconds": round(time.monotonic() - self.started, 2),
                "admission": "fifo-per-provider", "providers": {
                    name: {"capacity": capacity, "active": self.active[name],
                        "waiting": len(self.queues[name]), "admitted": self.admitted[name],
                        "mean_queue_seconds": round(self.wait_seconds[name] / self.admitted[name], 2)
                            if self.admitted[name] else 0,
                        "max_queue_seconds": round(self.max_wait_seconds[name], 2)}
                    for name, capacity in self.limits.items()}}


def configure_provider_admission(author_concurrency, review_concurrency, retained=None):
    """Bind every in-process transport before starting any controller threads."""
    from pipeline import provider
    from pipeline.author import transport as designer
    from pipeline.calibrate import agent, agent_openai
    from pipeline.select import classify
    retained = retained or {}
    elapsed = retained.get("elapsed_seconds", 0)
    if retained.get("at"):
        elapsed += max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(retained["at"])).total_seconds())
    admission = ProviderAdmission({"anthropic": author_concurrency, "openai": review_concurrency},
        max_requests=int(os.environ.get("ARENA_MAX_API_REQUESTS", str(_config.MAX_API_REQUESTS))),
        spent_requests=retained.get("requests", 0), elapsed_seconds=elapsed)
    provider.GLOBAL = admission
    provider.request_slot = designer.request_slot = agent.request_slot = classify.request_slot = admission.slot
    agent_openai.request_slot = lambda: admission.slot("openai")
    return admission


def authentication_failure(value):
    return bool(re.search(r"\bHTTP\s+(401|403)\b", str(value), re.IGNORECASE))


def provider_failure(value):
    """A rejected API admission is not evidence that a source is inadequate."""
    return (authentication_failure(value)
        or bool(re.search(r"\bHTTP\s+429\b", str(value), re.IGNORECASE))
        or "provider request budget exhausted" in str(value)
        # Receipts written before the wall-clock deadline was removed.
        or "provider request or wall-clock budget exhausted" in str(value)
        or "quota or spend limit; retry cannot restore access" in str(value)
        or any(text in str(value) for text in (
            "Anthropic call exceeded its deadline", "Anthropic call deadline expired",
            "Anthropic retry delay exceeds the remaining call deadline",
            "Anthropic transport interrupted", "OpenAI transport interrupted")))


def refresh_anthropic_credentials(path):
    """Adopt credential rotations without retaining secret values in receipts."""
    path = Path(path)
    if not path.is_file():
        return False
    names = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() in names:
            values[name.strip()] = value.strip().strip('"').strip("'")
    if not any(values.values()):
        raise RuntimeError("keys.env contains no Anthropic credential")
    # Set new values before removing stale alternatives, including an inherited
    # OAuth token that would otherwise take priority over an updated API key.
    for name, value in values.items():
        if value:
            os.environ[name] = value
    for name in names:
        if not values.get(name):
            os.environ.pop(name, None)
    return True


def read(path):
    return json.loads(Path(path).read_text())


def now():
    return datetime.now(timezone.utc).isoformat()


def source_key(row):
    return f"{row['session_id']}-{row['seed_index']}"


def excluded_families(roots):
    """Use provenance, never a slug or a task count, to identify earlier work."""
    from pipeline.config import bundle_paths
    found = {}
    for root in roots:
        bundles = set(bundle_paths(root)) | {p.parent for p in Path(root).glob("*/task.toml")}
        for bundle in sorted(bundles):
            spec, ledger = bundle / "authoring/spec.json", bundle / "provenance/ledger.json"
            path = spec if spec.is_file() else ledger
            if not path.is_file():
                raise ValueError(f"Cannot establish source family for existing task: {bundle}")
            session = read(path).get("source", {}).get("session_id")
            if not session:
                raise ValueError(f"Missing source session in {path}")
            found.setdefault(session, []).append(str(bundle.resolve()))
    return found


def ranked_candidates(rows, excluded):
    from pipeline.select.categories import categorise
    seen, result = set(), []
    # Tags order content inspection; they never approve or reject scope.
    for row in sorted(rows, key=lambda r: (r.get("tier", 4),
            not r.get("kw_tagged", False), -r.get("dispute", 0),
            -r.get("deliv_bytes", 0), r["session_id"], r["seed_index"])):
        key = source_key(row)
        if key in seen or row["session_id"] in excluded:
            continue
        seen.add(key)
        row = deepcopy(row)
        row["category"] = categorise(row)
        row["_rank"] = len(result) + 1
        result.append(row)
    return result


def allocate(rows, remaining, used):
    """Maximum matching prevents one category taking another's only family."""
    pools = {c: {} for c in CATEGORIES}
    for row in rows:
        if row["session_id"] not in used and row.get("classification", {}).get("survives"):
            pools[row["category"]].setdefault(row["session_id"], row)
    slots = [(c, i) for c in sorted(CATEGORIES, key=lambda c: len(pools[c]))
             for i in range(remaining[c])]
    owners = {}

    def assign(slot, seen):
        for session in pools[slot[0]]:
            if session in seen:
                continue
            seen.add(session)
            if session not in owners or assign(owners[session], seen):
                owners[session] = slot
                return True
        return False

    for slot in slots:
        assign(slot, set())
    return sorted((pools[slot[0]][sid] for sid, slot in owners.items()),
                  key=lambda row: row["_rank"])


def step_count_problems(bundle, expected):
    if expected is None:
        return []
    from pipeline.author.contracts import compile_steps
    try:
        steps = compile_steps(read(Path(bundle) / "authoring/spec.json"))
        if len(steps) != expected:
            return [f"Batch requires exactly {expected} steps; found {len(steps)}"]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return [f"Cannot verify batch step count: {exc}"]
    return []


def receipt_problems(bundle, receipt, policy, required_steps=None):
    from pipeline.artifacts import identity
    errors = step_count_problems(bundle, required_steps)
    if receipt.get("status") != "accepted" or receipt.get("release_ready") is not True:
        errors.append("task has no accepted release")
    if receipt.get("artifact_sha256") != identity(bundle):
        errors.append("release does not bind current task bytes")
    if receipt.get("policy_sha256") != policy:
        errors.append("release does not bind this pipeline policy")
    return errors


def external_qualification(work, descriptor):
    """Join a retained qualification without launching a duplicate campaign."""
    from pipeline.author.contracts import contained
    result = contained(work, descriptor["result"])
    while not result.is_file():
        if time.time() >= descriptor["deadline"]:
            raise RuntimeError("Retained qualification exceeded its original deadline")
        try:
            os.kill(descriptor["pid"], 0)
        except ProcessLookupError as exc:
            raise RuntimeError("Retained qualifier stopped without a final receipt") from exc
        time.sleep(5)
    return read(result)


def gate_repair_budget(request):
    """Targeted gate repairs this batch allows per candidate.

    Defaults to `policy.MAX_REPAIR_PASSES`. This was hard-coded to 0, and
    one-shot authoring is how 47 of 86 candidates were discarded holding
    findings that named the exact file and line. A frozen request keeps the
    budget it was created with, including an explicit 0.
    """
    budget = ((request or {}).get("repair_budget") or {}).get("targeted_gate_repairs")
    budget = _policy.MAX_REPAIR_PASSES if budget is None else budget
    if type(budget) is not int or budget < 0:
        raise ValueError("Invalid targeted gate repair budget")
    return budget


def snapshot(root, work, out, per_category, gate_repairs=None):
    from pipeline.artifacts import atomic_json, policy_identity
    from pipeline.cli import load_keys
    if gate_repairs is not None and (type(gate_repairs) is not int or gate_repairs < 0):
        raise ValueError("Gate repair budget must be a nonnegative integer")
    if (work / "request.json").exists():
        request = read(work / "request.json")
        if request["out"] != str(out) or request["per_category"] != per_category:
            raise ValueError("Resume parameters differ from the frozen batch request")
        if gate_repairs is not None and gate_repairs != gate_repair_budget(request):
            raise ValueError("Resume parameters differ from the frozen batch request")
        if request["controller_sha256"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
            raise ValueError("Controller changed; use a fresh artifact workspace")
        load_keys(prompt_if_missing=False)
        return request
    if out.exists() and any(out.iterdir()):
        raise ValueError("A new batch requires an empty output directory")
    work.mkdir(parents=True, exist_ok=True)
    code = work / "code"
    code.mkdir(exist_ok=False)
    before = policy_identity()
    for name in ("pipeline", "rulesets"):
        shutil.copytree(root / name, code / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("CLAUDE.md", "requirements-native.lock", "requirements-evidence.lock",
                 "pyproject.toml", "uv.lock", "arena", "workspace.toml", "category_batch.py"):
        shutil.copy2(root / name, code / name)
    if before != policy_identity():
        raise RuntimeError("Pipeline changed while creating the snapshot; use a fresh workspace")
    frozen_policy = subprocess.check_output([sys.executable, "-c",
        "from pipeline.artifacts import policy_identity; print(policy_identity())"],
        cwd=code, text=True).strip()
    existing = [p for p in root.glob("envs*") if p.is_dir() and p.resolve() != out]
    existing += [root / "reference", root / "reference/prior-delivery"]
    exclusions = excluded_families(existing)
    queue = ranked_candidates(read(root / "pipeline-artifacts/tiered.json")["rows"], exclusions)
    out.mkdir(parents=True, exist_ok=True)
    for folder in CATEGORIES.values():
        (out / folder).mkdir(exist_ok=True)
    for peer in [*existing, out]:
        link = code / peer.name
        if peer.exists() and not link.exists():
            link.symlink_to(peer.resolve(), target_is_directory=True)
    candidates = work / "candidates"
    candidates.mkdir(exist_ok=True)
    # Reuse content-addressed classification results without sharing mutable
    # reports, native campaigns, or model budgets with another batch.
    cache = root / "pipeline-artifacts/classify-cache"
    if cache.exists():
        shutil.copytree(cache, work / "classify-cache")
    request = {"schema": "arena-pipeline/category-batch/1", "created_at": now(),
        "out": str(out), "work": str(work), "code": str(code),
        "per_category": per_category, "required_steps": 2, "policy_sha256": frozen_policy,
        "controller_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        # Calibration models and effort come from `pipeline.policy`, never from
        # a literal here: a stale default buried in a snapshot is how a batch
        # gets measured on a model nobody chose.
        "models": list(_policy.resolve_calibration_models()),
        "calibration_effort": _policy.SPEC_CALIBRATION_EFFORT,
        "no_limits": _policy.SPEC_NO_LIMITS,
        "topup_attempts": _policy.MAX_TOPUP_ATTEMPTS,
        "rollouts_per_model": 10,
        "excluded_families": exclusions, "requested": per_category * len(CATEGORIES),
        "publication": "accepted exact versions only; no category backfilling",
        "hardening_mode": "manual",
        "repair_budget": {"whole_design_attempts": 1,
                          "targeted_gate_repairs": (_policy.MAX_REPAIR_PASSES
                                                    if gate_repairs is None else gate_repairs),
                          "qualification_repairs": 0},
        "source_capacity": {c: len({r['session_id'] for r in queue if r['category'] == c})
                            for c in CATEGORIES}}
    atomic_json(work / "queue.json", {"rows": queue})
    atomic_json(work / "request.json", request)
    load_keys(prompt_if_missing=False)
    return request


from pipeline.author.quality import BAND_LIMITS  # noqa: E402  the brief states the enforced limits

DESIGN_BRIEF = [
    "Design a genuinely new, extremely high-quality and difficult professional task. Inspect the complete real request, clarifications and evidence before choosing the question.",
    "Build the complete environment before independent semantic review. Use existing design findings as feedback; no preliminary plan approval is required. Finish the staged draft, run check_draft and submit_draft.",
    "You may add legitimate authored traps grounded in the trace scenario, even when no matching user correction occurred. Cite the supporting fixture, explain the plausible professional error, and demonstrate the wrong-answer probe and correct solution. Public evidence must resolve the trap; no hidden rules or unrelated distractions. Disclose authored additions and preserve category fidelity.",
    "Every environment in this batch must have exactly TWO graded steps. Step two must require a consequential source-supported revision or follow-on decision, with explicit output ownership and its own substantive grading. Preserve the required independent core decisions; duplicated work or formatting is not a second professional step.",
    f"Specify {BAND_LIMITS['core'][0]}–{BAND_LIMITS['core'][1]} independent consequential core decisions and {BAND_LIMITS['stretch'][0]}–{BAND_LIMITS['stretch'][1]} stretch decisions before choosing serialization fields. Difficulty must come from source-supported joins, authority arbitration, coupled constraints, temporal revision and justified restraint. Output volume, hidden rules, keyword extraction and ambiguity are not difficulty.",
    "Check each reference target, alternative correct answer, plausible wrong decision and source counterfactual by execution before submission. Preserve complete keyed records and occurrence counts. State boundary rules and rounding conventions publicly.",
    "This batch excludes earlier source families and allows only one task per new family. Read the comparison corpus. A renamed or reskinned question does not establish novelty.",
    "Preserve recovered evidence and category provenance. Disclose and charge every authored supplement. Reject an inadequate source with cited evidence instead of weakening quality, truth, privacy or fidelity requirements.",
    "One initial whole-design attempt is available. Produce a high-quality, hard task in that first attempt: use the exploration budget to construct and verify consequential decisions and scenario-grounded traps before submission. Do not defer difficulty or correctness to a later repair round. Concrete gate findings on the submitted draft return to you for a bounded number of targeted repair passes; each pass must strictly shrink the finding set, and a repeated finding ends the attempt. Difficulty hardening runs only on tasks explicitly selected by the user. Empirical difficulty remains unproven until fresh native qualification.",
    "Before building a large spec, audit the complete public fixture for unnecessary personal names, contact details and live external assets. Recovered provenance does not establish publication permission. Recovered files are immutable through your tools: if required source material has unresolved contextual privacy that the existing supported minimization has not fixed, reject_seed with grounded evidence early. Do not spend the design budget on a task that cannot pass publication review, silently drop required evidence, or claim a redaction that was never applied.",
    "Match the professional engagement to exactly what the deliverable and grader can assess. Do not promise a comprehensive audit, plan or clearance and grade only a convenient subset. Every consequential claim needs a source-supported rule and substantive coverage. Use a narrower, well-supported engagement only when it still serves the original request and supports four independent core decisions.",
    "For every selection rule, evaluate boundary cases inside AND outside the declared set or interval. Exact windows such as weeks {3,4} are not equivalent to >=3; ranking offsets need a published bound or a source-derived sentinel. Use scope-aware source parsing: a word in an unrelated section must not establish a policy, constraint or missing-evidence conclusion. Add source invariants for irrelevant text and source counterfactuals for the actual governing facts.",
    "Use public, enforceable obligations only. Check the instruction against the exported multi-step contract: declared prior-step outputs may coexist, so do not invent conflicting per-step file prohibitions. Test the real protected state for a guard rather than merely comparing an answer field. Exact names, copied labels, all-true flags and empty sets cannot supply the core decision budget.",
    "When the user explicitly requests a revision, read the complete feedback before changing anything. Make a finite checklist linking each reported defect to its source rule, instruction or grader change, and an executed check. A clean mechanical check_draft is necessary but does not resolve an independent semantic finding. Address every finding within the retained repair allowance; do not tune to individual solver answers.",
]


class Controller:
    def __init__(self, request, classify_workers, task_workers, qualification_workers=2, native_workers=2):
        from pipeline.artifacts import policy_identity
        from pipeline.orchestrate import Budget
        self.request = request
        self.work, self.out = Path(request["work"]), Path(request["out"])
        self.policy = request["policy_sha256"]
        if policy_identity() != self.policy:
            raise ValueError("Frozen implementation does not match requested policy")
        self.queue = read(self.work / "queue.json")["rows"]
        self.classify_workers, self.task_workers = classify_workers, task_workers
        self.qualification_workers, self.native_workers = qualification_workers, native_workers
        self.lock = threading.RLock()
        # Author conversations may do local work concurrently. Actual API calls
        # use separate FIFO provider queues with a shared spend bound.
        self.budget = Budget(task_workers + qualification_workers)
        self.records = {}
        self.classified = {}
        for path in (self.work / "classifications").glob("*.json"):
            record = read(path)
            self.classified[source_key(record["row"])] = record
        for path in (self.work / "tasks").glob("*.json"):
            record = read(path)
            # A screen that did not measure cleanly is retried once per
            # controller run: back to `validated`, so qualification screens it
            # again (reusing every campaign whose evidence verified). Not in
            # QUALIFICATION_STAGES, so a still-incomplete screen cannot spin.
            if record.get("stage") == "screen-incomplete":
                record["stage"] = "validated"
            # Parked until the operator relaunches its short campaign. Once the
            # current screen no longer says the budget is spent (the relaunch
            # ran, or the task changed and needs a fresh screen), it rejoins
            # qualification, which re-reads that screen before spending anything.
            elif record.get("stage") == "screen-needs-operator" and record.get("candidate"):
                from pipeline import screen as _screen
                if not (_screen.current(Path(record["candidate"])) or {}).get("topup_exhausted"):
                    record["stage"] = "validated"
            self.records[source_key(record["row"])] = record
        from pipeline import checkpoint as progress
        from pipeline.artifacts import atomic_json
        for key, record in self.records.items():
            authorized = validity_authorization(request, key)
            if record['stage'] == 'validity-queued':
                if not authorized:
                    raise ValueError('Queued validity revision has no explicit authorization')
                continue
            if (authorized and record['stage'] in VALIDITY_STAGES and validity_findings(record)
                    and validity_passes_left(Path(record['candidate']), authorized)):
                record.update(stage='validity-queued', manual_trigger_required=False)
                atomic_json(self.work / 'tasks' / (key + '.json'), record)
                continue
            if record['stage'] not in {'building', 'provider-unavailable', 'error', 'unverified', *AUTHOR_RESUME_STAGES}:
                continue
            candidate = Path(record['candidate']) if record.get('candidate') else None
            if candidate is None:
                continue
            try:
                saved = progress.resumable(candidate)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                record.update(stage='interrupted', error=str(exc))
            else:
                if not saved:
                    if record['stage'] not in {'building', *AUTHOR_RESUME_STAGES}:
                        continue
                    record.update(stage='interrupted', error='No safe pipeline checkpoint; retained workspace needs inspection')
                else:
                    if saved.get('feedback') and saved['stage'] in {'authoring', 'repair'} and not authorized:
                        record.update(stage='awaiting-hardening',
                            error='Saved revision requires an explicit manual hardening trigger')
                        atomic_json(self.work / 'tasks' / (key + '.json'), record)
                        continue
                    record.update(stage='resume-session' if saved['stage'] == 'authoring' else 'resume-build',
                        gate_repairs_remaining=saved.get('gate_repairs_remaining', gate_repair_budget(request)),
                        qualification_repairs_remaining=saved.get('qualification_repairs_remaining', 1))
                    record['authoring_session'] = saved.get('authoring_session') or {}
            atomic_json(self.work / 'tasks' / (key + '.json'), record)
        self.phase = "starting"
        self.provider_error = None
        self.publication_claims = {}

    def note_provider_failure(self, error):
        if provider_failure(error):
            with self.lock:
                self.provider_error = ("provider authentication failed" if authentication_failure(error)
                    else "provider transport unavailable" if any(word in str(error) for word in ('deadline', 'transport interrupted'))
                    else "provider rate limit or budget exhausted")

    def update(self, row, stage, **fields):
        from pipeline.artifacts import atomic_json
        with self.lock:
            key = source_key(row)
            self.records.setdefault(key, {"row": row, "created_at": now()})
            if stage == 'building':
                # Keep recovered errors as history, not as a current failure.
                previous = {name: self.records[key].pop(name) for name in ('error', 'build')
                            if name in self.records[key]}
                if previous:
                    self.records[key].setdefault('prior_outcomes', []).append({
                        'at': now(), **previous})
            self.records[key].update(stage=stage, updated_at=now(), **fields)
            atomic_json(self.work / "tasks" / (key + ".json"), self.records[key])
            self.save()
            print(row["category"], key[:12], stage, flush=True)

    def counts(self):
        return Counter(r["row"]["category"] for r in self.records.values()
                       if r["stage"] == "accepted")

    def save(self):
        from pipeline.artifacts import atomic_json
        from pipeline.provider import GLOBAL
        from pipeline.author.transport import CALL_DEADLINE_S
        from math import isfinite
        with self.lock:
            counts = self.counts()
            report = {"schema": "arena-pipeline/category-batch-status/1", "at": now(),
                "phase": self.phase, "pid": os.getpid(), "policy_sha256": self.policy,
                "requested_per_category": self.request["per_category"],
                "accepted": {CATEGORIES[c]: counts[c] for c in CATEGORIES},
                "classified": len(self.classified),
                "surviving_sources": sum(bool(r["row"].get("classification", {}).get("survives"))
                                         for r in self.classified.values()),
                "stages": dict(Counter(r["stage"] for r in self.records.values())),
                "provider": GLOBAL.stats(), "tasks": list(self.records.values()),
                "authoring_call_deadline_seconds": CALL_DEADLINE_S if CALL_DEADLINE_S is not None and isfinite(CALL_DEADLINE_S) else None,
                "complete": self.phase == "complete" and all(
                    counts[c] == self.request["per_category"] for c in CATEGORIES)}
            report["capacity"] = {"authoring": self.task_workers,
                "screening": self.classify_workers,
                "qualification": self.qualification_workers,
                "native_episodes_per_qualification": self.native_workers,
                "provider_requests": int(os.environ.get("ARENA_PROVIDER_CONCURRENCY", str(_config.MAX_PROVIDER_CONCURRENCY))),
                "review_provider_requests": int(os.environ.get("ARENA_REVIEW_PROVIDER_CONCURRENCY", str(_config.MAX_PROVIDER_CONCURRENCY)))}
            report["required_steps"] = self.request.get("required_steps")
            report["hardening_mode"] = "manual"
            report['validity_revision'] = ({k: self.request['validity_revision'][k]
                for k in ('id', 'purpose', 'max_passes', 'max_steps')}
                if self.request.get('validity_revision') else None)
            import socket
            report["execution_host"] = socket.gethostname()
            report["workspace"] = str(Path(__file__).resolve().parent)
            report["selected_only"] = self.request.get("selected_only", False)
            from pipeline.benchmark.capacity import NATIVE_CAPACITY
            report["capacity"]["native_episode_slots"] = NATIVE_CAPACITY.limit
            report["native_capacity"] = NATIVE_CAPACITY.stats()
            from pipeline.local_work import stats as local_stats
            report['local_workers'] = local_stats()
            phases = Counter()
            for record in self.records.values():
                if record['stage'] == 'qualifying' and record.get('candidate'):
                    progress = Path(record['candidate']) / 'verification/qualification.json'
                    phases[read(progress).get('stage', 'verification') if progress.exists() else 'preflight'] += 1
            report['qualification_stages'] = dict(phases)
            report['calibrated_candidates'] = sum(r['stage'] == 'accepted' or bool(r.get('measurement', {}).get('complete'))
                                                for r in self.records.values())
            if self.request.get("deadline"):
                deadline = datetime.fromisoformat(self.request["deadline"])
                hours = (deadline - datetime.now(timezone.utc)).total_seconds() / 3600
                outstanding = sum(max(0, self.request["per_category"] - counts[c]) for c in CATEGORIES)
                report["deadline"] = {"at": deadline.isoformat(), "remaining_hours": round(hours, 2),
                    "outstanding": outstanding,
                    "required_qualified_per_hour": round(outstanding / hours, 2) if hours > 0 else None,
                    "overdue": hours <= 0 and outstanding > 0}
            atomic_json(self.work / "status.json", report)
            lines = ["# envs-cat-v2 pipeline status", "", f"Updated: {report['at']}",
                     f"Phase: {self.phase}", "", "| Category | Qualified | Requested |",
                     "| --- | ---: | ---: |"]
            lines += [f"| {CATEGORIES[c]} | {counts[c]} | {self.request['per_category']} |" for c in CATEGORIES]
            lines += ["", f"Classified candidates: {len(self.classified)}.",
                      f"Active authors: {report['stages'].get('building', 0)}; tasks in qualification: {report['stages'].get('qualifying', 0)}.",
                      f"Qualification stages: {dict(phases)}; native slots reserved: {report['native_capacity']['reserved']}.",
                      f"Candidates with two completed model calibrations: {report['calibrated_candidates']}.",
                      f"Validity corrections queued: {report['stages'].get('validity-queued', 0)}; unresolved: {report['stages'].get('validity-blocked', 0)}.",
                      f"Hardening: manual only; {report['stages'].get('awaiting-hardening', 0)} held for later review.",
                      f"Screens needing an operator relaunch (`arena screen --relaunch-incomplete`): {report['stages'].get('screen-needs-operator', 0)}.",
                      "Only accepted exact task versions occupy the category folders.",
                      f"Candidate outcomes and evidence: `{self.work}`.", ""]
            if "deadline" in report:
                delivery = report["deadline"]
                lines += [f"Delivery deadline: {delivery['at']}.",
                    f"Hours remaining: {delivery['remaining_hours']}; tasks outstanding: {delivery['outstanding']}.",
                    f"Required qualified tasks per hour: {delivery['required_qualified_per_hour']}.",
                    "This required rate is a target, not a completion forecast.", ""]
            (self.out / "STATUS.md").write_text("\n".join(lines))

    def classify(self, row):
        from pipeline.select.classify import classify_seed, summarise, TOOL
        from pipeline.author.contracts import schema_errors
        from pipeline.artifacts import atomic_json
        row = deepcopy(row)
        try:
            verdict = classify_seed(row, use_cache=True)
            schema = TOOL["input_schema"]
            problems = schema_errors({k: v for k, v in verdict.items() if k in schema["properties"]}, schema)
            if problems:
                raise ValueError("Invalid classification: " + "; ".join(problems))
            row["classification"] = summarise(row, verdict)
            record = {"row": row, "verdict": verdict, "at": now()}
        except Exception as exc:
            record = {"row": row, "error": f"{type(exc).__name__}: {exc}", "at": now()}
            self.note_provider_failure(exc)
        with self.lock:
            self.classified[source_key(row)] = record
            atomic_json(self.work / "classifications" / (source_key(row) + ".json"), record)
            self.save()
        print("classified", row["category"], source_key(row)[:12],
              "unverified" if "error" in record else row["classification"]["survives"], flush=True)
        return record

    def import_prefetched(self):
        """Admit completed, policy-bound prefetch work without repeating calls."""
        from pipeline.artifacts import atomic_json
        from pipeline.author.contracts import schema_errors
        from pipeline.select.classify import summarise, TOOL
        from pipeline.dump.evidence import SCHEMA
        manifest = self.work / "prefetch/request.json"
        if not manifest.exists():
            return
        request = read(manifest)
        if request.get("policy_sha256") != self.policy:
            raise ValueError("Prefetched classifications belong to another policy")
        originals = {source_key(row): row for row in self.queue}
        requested = {source_key(row) for row in request["rows"]}
        for path in sorted((self.work / "prefetch/results").glob("*.json")):
            record = read(path)
            key = source_key(record["row"])
            if key in self.classified:
                continue
            if key not in originals or key not in requested:
                raise ValueError("Prefetch returned an unrequested source")
            row = deepcopy(originals[key])
            if any(record["row"].get(k) != v for k, v in row.items()):
                raise ValueError("Prefetched source metadata changed")
            imported = {"row": row, "at": now(), "prefetched": True}
            if "error" in record:
                imported["error"] = record["error"]
            else:
                verdict, schema = record["verdict"], TOOL["input_schema"]
                problems = schema_errors({k: v for k, v in verdict.items() if k in schema["properties"]}, schema)
                if (problems or verdict.get("evidence_schema") != SCHEMA or
                        verdict.get("source_evidence_sha256") != row.get("source_evidence_sha256")):
                    raise ValueError("Prefetch verdict has invalid or stale source evidence")
                row["classification"] = summarise(row, verdict)
                imported["verdict"] = verdict
            with self.lock:
                self.classified[key] = imported
                atomic_json(self.work / "classifications" / (key + ".json"), imported)

    def build(self, row):
        from pipeline.author import agent_designer
        from pipeline.orchestrate import build_one, _slug
        row = deepcopy(row)
        candidate = self.work / "candidates" / CATEGORIES[row["category"]] / _slug(row, row["_rank"])
        key = source_key(row)
        prior = self.records.get(key, {})
        authorized = validity_authorization(getattr(self, 'request', {}), key)
        try:
            if getattr(self, "provider_error", None):
                self.update(row, "provider-unavailable", candidate=str(candidate), error=self.provider_error)
                return
            if prior.get("stage") not in QUALIFICATION_STAGES:
                from pipeline import checkpoint as progress
                if prior.get('stage') == 'validity-queued':
                    if not authorized:
                        raise ValueError('Validity revision requires explicit authorization')
                    prepare_validity_revision(candidate, authorized, validity_findings(prior))
                saved = progress.resumable(candidate)
                if prior and candidate.exists() and saved is None:
                    self.update(row, "interrupted", error="No verified resumable checkpoint; retained workspace needs inspection")
                    return
                self.update(row, "building", candidate=str(candidate))

                # `budget` and the orchestrator's options are accepted: the
                # build calls every designer with them, and this signature
                # used to reject `budget`, so every build here failed to design.
                def designer(source, extraction, bundle, feedback=None, budget=None, **orchestrated):
                    retained = progress.resumable(bundle)
                    checkpoint = retained.get('authoring_session') if retained and not feedback else None
                    draft = bundle / "authoring/spec.json"
                    initial = (progress.submitted_spec(bundle, retained) if feedback and retained
                               and retained.get('stage') == 'repair' else
                               read(draft) if feedback and draft.exists() else None)
                    options = ({'resume_session': checkpoint, 'max_steps': checkpoint['remaining_steps']}
                               if checkpoint else {'initial_spec': initial})
                    if authorized and feedback and not checkpoint:
                        options['max_steps'] = authorized['max_steps']
                    return agent_designer.design(source, extraction, bundle, budget=None,
                        feedback=DESIGN_BRIEF + list(feedback or []), **options,
                        # The build decides: a gate or validity repair is a revision; a
                        # stalled first design's continuation is still the first design.
                        revision=bool(orchestrated.get('revision', bool(feedback))))

                # A retained record never regains a repair it already spent.
                gate_repairs = gate_repair_budget(getattr(self, 'request', {}))
                spent = prior.get('gate_repairs_remaining')
                if type(spent) is int and 0 <= spent < gate_repairs:
                    gate_repairs = spent
                result = build_one(row, row["_rank"], self.work / "candidates", designer,
                    self.budget,
                    gate_repairs=gate_repairs, **({'preserve_workspace': True} if saved else {}))
                self.note_provider_failure(result.error)
                if provider_failure(result.error):
                    self.update(row, "provider-unavailable", build=asdict(result), candidate=str(candidate))
                    return
                stage = 'awaiting-hardening' if result.status == 'blocked' else result.status
                if authorized and result.status != 'error':
                    retained = progress.read(candidate).get('validity_revision', {})
                    if retained.get('authorization_id') == authorized['id']:
                        progress.update(candidate, validity_revision={**retained, 'active': False})
                    if result.status in {'blocked', 'unverified'}:
                        actionable = validity_findings({'build': asdict(result)})
                        stage = ('validity-queued' if actionable and validity_passes_left(candidate, authorized)
                                 else 'validity-blocked')
                self.update(row, stage, build=asdict(result), candidate=str(candidate))
                if result.status != "validated":
                    return
            # Native campaigns have their own bounded pool. A validated draft
            # reserves a category slot while freeing its authoring worker.
        except Exception as exc:
            self.note_provider_failure(exc)
            self.update(row, "provider-unavailable" if provider_failure(exc) else "unverified",
                        error=f"{type(exc).__name__}: {exc}")

    def qualify(self, row):
        from pipeline.release import qualify, evaluate
        from pipeline.artifacts import identity, atomic_json
        from pipeline.orchestrate import _slug
        key = source_key(row)
        publication_claimed = False
        prior = self.records[key]
        candidate = Path(prior.get("candidate") or
            self.work / "candidates" / CATEGORIES[row["category"]] / _slug(row, row["_rank"]))
        try:
            if prior.get("stage") not in QUALIFICATION_STAGES:
                raise ValueError("Only a validated draft can enter qualification")
            if getattr(self, "provider_error", None):
                self.update(row, "provider-unavailable", candidate=str(candidate), error=self.provider_error)
                return
            if issues := step_count_problems(candidate, self.request.get("required_steps")):
                self.update(row, "qualification-rejected", errors=issues)
                return
            self.update(row, "qualifying")
            repairs = prior.get("qualification_repairs_remaining", 1)
            if type(repairs) is not int or not 0 <= repairs <= 1:
                raise ValueError("Invalid retained qualification repair budget")
            if prior.get("external_qualification"):
                release = external_qualification(self.work, prior["external_qualification"])
            else:
                # The ten-episode calibration of record is spent only on a task
                # whose five-episode screen is shippable (release._qualify
                # refuses otherwise). Screen first; a task above the target is
                # held for manual hardening with its scores, not qualified.
                from pipeline import screen
                from pipeline.release import _calibrated
                screened = {"shippable": True}
                if not _calibrated(candidate, self.request["models"], 10):
                    screened = screen.current(candidate)
                    if not screened or not screened.get("measurement_complete"):
                        # Scored campaigns with verified evidence are reused
                        # inside screen.run; only what measured nothing re-runs.
                        screened = screen.run(
                            candidate, native_root=self.work / "native-screen" / key,
                            models=self.request["models"], label="category-batch")
                # Only a screen that did not measure cleanly holds a task; its
                # pass@1 never does (pass@1 < 0.5 is a target, not a gate).
                if not screened.get("shippable"):
                    # A campaign short of episodes with its void budget spent is
                    # not retried on every restart: only an operator relaunch
                    # (which re-runs scored episodes) can complete it.
                    stuck = bool(screened.get("topup_exhausted"))
                    self.update(row, "screen-needs-operator" if stuck else "screen-incomplete", screen={
                        **({"remedy": screened.get("remedy")} if stuck else {}),
                        "outlook": (screened.get("outlook") or {}).get("verdict"),
                        "problems": screened.get("shippable_problems", [])[:6],
                        "per_model": {m: {k: r.get(k) for k in ("pass_at_1", "pass_at_4", "mean_reward")}
                                      for m, r in (screened.get("per_model") or {}).items()}})
                    return
                _reviewer = self.request["models"][-1]
                release = qualify(candidate, self.request["models"], _reviewer, _reviewer,
                                  self.work / "native" / key, n=10,
                                  workers=getattr(self, "native_workers", 2), repair_attempts=0)
            self.note_provider_failure(release.get("errors", []))
            if provider_failure(release.get("errors", [])):
                self.update(row, "provider-unavailable", release=release)
                return
            issues = receipt_problems(candidate, release, self.policy, self.request.get("required_steps"))
            if issues:
                stage = ('awaiting-hardening' if release.get('status') in {'blocked', 'reserve', 'awaiting-hardening'}
                         else 'qualification-rejected')
                fields = {}
                authorized = validity_authorization(self.request, key)
                findings = review_validity_findings(candidate) if authorized else []
                if findings:
                    fields['validity_findings'] = findings
                    stage = ('validity-queued' if validity_passes_left(candidate, authorized) else 'validity-blocked')
                elif measurement := completed_calibration(candidate, self.request['models']):
                    fields['measurement'] = measurement
                    stage = 'calibrated-awaiting-review'
                self.update(row, stage, release=release, errors=issues, **fields)
                return
            with self.lock:
                claims = getattr(self, 'publication_claims', {})
                self.publication_claims = claims
                occupied = self.counts()[row['category']] + sum(c == row['category'] for c in claims.values())
                if occupied >= self.request['per_category']:
                    self.update(row, 'qualified-reserve', release=release)
                    return
                claims[key] = row['category']
                publication_claimed = True
            destination = self.out / CATEGORIES[row["category"]] / candidate.name
            if destination.exists():
                if identity(destination) != identity(candidate):
                    raise ValueError("Refusing to overwrite an existing output task")
            else:
                shutil.copytree(candidate, destination)
            checked = evaluate(destination)
            issues = receipt_problems(destination, checked, self.policy, self.request.get("required_steps"))
            if issues:
                quarantine = self.work / "publication-rejected" / key
                quarantine.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(destination), quarantine)
                self.update(row, "publication-rejected", release=checked, errors=issues)
                return
            atomic_json(destination / "verification/batch-selection.json", {
                "source_key": key, "category": row["category"], "rank": row["_rank"],
                "request": str(self.work / "request.json"), "verified_at": now()})
            # Verification metadata is outside the frozen task identity.
            if identity(destination) != checked["artifact_sha256"]:
                raise ValueError("Publication metadata unexpectedly changed task identity")
            self.update(row, "accepted", bundle=str(destination), release=checked)
        except Exception as exc:
            self.note_provider_failure(exc)
            self.update(row, "provider-unavailable" if provider_failure(exc) else "unverified",
                        error=f"{type(exc).__name__}: {exc}")
        finally:
            if publication_claimed:
                with self.lock:
                    self.publication_claims.pop(key, None)

    def finalize(self):
        """Requalify against the complete, stable corpus before closing a batch.

        A later peer changes review_context even when earlier task bytes are
        unchanged. Refresh only stale reviews; retain exact native evidence.
        Collect every result before removing a rejected peer so concurrent
        reviews all inspect the same corpus.
        """
        from pipeline.release import qualify, evaluate
        self.phase = "final-corpus-review"
        self.save()
        records = [r for r in self.records.values() if r["stage"] == "accepted"]

        def verify(record):
            bundle = Path(record["bundle"])
            _reviewer = self.request["models"][-1]
            release = qualify(bundle, self.request["models"], _reviewer, _reviewer,
                self.work / "native-final" / source_key(record["row"]),
                n=10, workers=getattr(self, "native_workers", 2), repair_attempts=0)
            return record, release, receipt_problems(bundle, release, self.policy, self.request.get("required_steps"))

        with ThreadPoolExecutor(max_workers=getattr(self, "qualification_workers", 2)) as pool:
            results = [future.result() for future in as_completed([pool.submit(verify, r) for r in records])]
        rejected = False
        for record, release, issues in results:
            if issues:
                destination = self.work / "batch-review-rejected" / source_key(record["row"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(record["bundle"], destination)
                self.update(record["row"], "batch-review-rejected", release=release,
                            errors=issues, retained_at=str(destination))
                rejected = True
            else:
                self.update(record["row"], "accepted", release=release)
        if rejected:
            return False
        # Catch external changes during the complete-corpus review. Recounting
        # old accepted flags alone can never close this batch.
        for record in records:
            bundle = Path(record["bundle"])
            receipt = evaluate(bundle)
            issues = receipt_problems(bundle, receipt, self.policy, self.request.get("required_steps"))
            if issues:
                raise RuntimeError("Final batch evidence changed: " + "; ".join(issues))
        return True

    def run(self):
        from pipeline.release import evaluate
        from pipeline.benchmark.harbor import doctor
        from pipeline.artifacts import atomic_json
        runtime = doctor()
        atomic_json(self.work / "runtime-preflight.json", runtime)
        if not runtime["ok"]:
            self.phase = "runtime-unavailable"
            self.save()
            return 2
        for record in list(self.records.values()):
            if record["stage"] == "accepted":
                bundle = Path(record["bundle"])
                release = evaluate(bundle)
                issues = receipt_problems(bundle, release, self.policy, self.request.get("required_steps"))
                if issues:
                    self.update(record["row"], "acceptance-stale", release=release, errors=issues)
                    self.phase = "acceptance-needs-repair"
                    self.save()
                    return 2
        return self.drive()

    def drive(self):
        """Balance categories across independent author and qualification pools."""
        pending_builds, pending_classifications, pending_qualifications = {}, {}, {}
        errors_in_a_row = 0
        with ThreadPoolExecutor(max_workers=self.task_workers) as builders, \
                ThreadPoolExecutor(max_workers=self.classify_workers) as classifiers, \
                ThreadPoolExecutor(max_workers=getattr(self, "qualification_workers", 2)) as qualifiers:
            while True:
                if getattr(self, "provider_error", None):
                    self.phase = ("provider-auth-unavailable" if self.provider_error == "provider authentication failed"
                                  else "provider-unavailable")
                    self.save()
                    return 2
                self.import_prefetched()
                with self.lock:
                    counts = self.counts()
                    pending_keys = {source_key(row) for row in
                                    [*pending_builds.values(), *pending_qualifications.values()]}
                    # Queued native work also reserves its category quota, but
                    # a build transitioning into qualification counts only once.
                    reserved = {source_key(row): row for row in
                                [*pending_builds.values(), *pending_qualifications.values()]}
                    reserved.update({source_key(r["row"]): r["row"] for r in self.records.values()
                                     if r["stage"] in QUALIFICATION_STAGES})
                    reserved = {key: row for key, row in reserved.items()
                                if self.records.get(key, {}).get("stage") != "accepted"}
                    active = Counter(row["category"] for row in reserved.values())
                    remaining = {c: max(0, self.request["per_category"] - counts[c] - active[c])
                                 for c in CATEGORIES}
                    if self.request.get('eager_selected', False):
                        # Finish the selected initial drafts while other candidates
                        # qualify. Only accepted releases consume publication quota.
                        remaining = {c: self.task_workers if counts[c] < self.request['per_category'] else 0
                                     for c in CATEGORIES}
                    used = {r["row"]["session_id"] for r in self.records.values()}
                    available = [r["row"] for r in self.classified.values() if "error" not in r]
                    resumable = [r["row"] for r in self.records.values()
                                 if r["stage"] in AUTHOR_RESUME_STAGES
                                 and source_key(r["row"]) not in pending_keys]
                    ready_to_qualify = [r["row"] for r in self.records.values()
                                       if r["stage"] in QUALIFICATION_STAGES
                                       and (r['stage'] != 'qualified-reserve' or
                                            counts[r['row']['category']] + sum(
                                                c == r['row']['category'] for c in getattr(self, 'publication_claims', {}).values())
                                            < self.request['per_category'])
                                       and source_key(r["row"]) not in pending_keys]
                qualifying_by_category = Counter(r["category"] for r in pending_qualifications.values())
                while ready_to_qualify and len(pending_qualifications) < getattr(self, "qualification_workers", 2):
                    row = min(ready_to_qualify, key=lambda r: (
                        counts[r["category"]] + qualifying_by_category[r["category"]], r["_rank"]))
                    ready_to_qualify.remove(row)
                    self.update(row, "qualification-queued")
                    pending_qualifications[qualifiers.submit(self.qualify, row)] = row
                    qualifying_by_category[row["category"]] += 1

                selected = allocate(available, remaining, used) + resumable
                resume_keys = {source_key(row) for row in resumable}
                active_families = {row["session_id"] for row in reserved.values()}
                while len(pending_builds) < self.task_workers:
                    choices = [r for r in selected if remaining[r["category"]] > 0
                               and r["session_id"] not in active_families]
                    if not choices:
                        break
                    # Recovery is preferred within an underserved category;
                    # recovery records cannot monopolize all authoring slots.
                    row = min(choices, key=lambda r: (
                        counts[r["category"]] + active[r["category"]],
                        source_key(r) not in resume_keys,
                        not bool(self.records.get(source_key(r), {}).get("authoring_session", {}).get("review_resume_sha256")),
                        -self.records.get(source_key(r), {}).get("authoring_session", {}).get("response_count", 0),
                        r["_rank"]))
                    selected = [r for r in selected if r["session_id"] != row["session_id"]]
                    if source_key(row) not in resume_keys:
                        self.update(row, "queued")
                    used.add(row["session_id"])
                    active_families.add(row["session_id"])
                    active[row["category"]] += 1
                    remaining[row["category"]] = max(0, remaining[row["category"]] - 1)
                    pending_builds[builders.submit(self.build, row)] = row

                if not pending_builds and not pending_qualifications and all(
                        counts[c] == self.request["per_category"] for c in CATEGORIES):
                    if self.finalize():
                        self.phase = "complete"
                        self.save()
                        return 0
                    continue

                if not self.request.get('selected_only', False) and errors_in_a_row < self.classify_workers * 2:
                    ready = {c: {r["session_id"] for r in available if r["category"] == c
                                 and r["session_id"] not in used and r.get("classification", {}).get("survives")}
                             for c in CATEGORIES}
                    while len(pending_classifications) < self.classify_workers:
                        inflight = Counter(r["category"] for r in pending_classifications.values())
                        reserved = {source_key(r) for r in pending_classifications.values()}
                        inspecting = {r["session_id"] for r in pending_classifications.values()}
                        eligible_categories = [c for c in CATEGORIES
                            if len(ready[c]) + inflight[c] < remaining[c]]
                        choices = []
                        for category in eligible_categories:
                            pick = next((r for r in self.queue if r["category"] == category
                                and source_key(r) not in self.classified and source_key(r) not in reserved
                                and r["session_id"] not in used and r["session_id"] not in inspecting), None)
                            if pick is not None:
                                choices.append(pick)
                        if not choices:
                            break
                        row = min(choices, key=lambda r: (counts[r["category"]] + active[r["category"]]
                                  + len(ready[r["category"]]) + inflight[r["category"]],
                                  r["category"], r["_rank"]))
                        pending_classifications[classifiers.submit(self.classify, row)] = row

                if not pending_builds and not pending_classifications and not pending_qualifications:
                    self.phase = ("classification-unavailable" if errors_in_a_row else "source-pool-exhausted")
                    self.save()
                    return 2 if errors_in_a_row else 1
                self.phase = ("building-and-qualifying" if pending_builds and pending_qualifications
                    else "building-and-screening" if pending_builds
                    else "qualifying" if pending_qualifications else "classifying-new-sources")
                self.save()
                done, _ = wait([*pending_builds, *pending_classifications, *pending_qualifications], timeout=15,
                               return_when=FIRST_COMPLETED)
                for future in done:
                    if future in pending_builds:
                        future.result()
                        del pending_builds[future]
                    elif future in pending_qualifications:
                        future.result()
                        del pending_qualifications[future]
                    else:
                        record = future.result()
                        errors_in_a_row = errors_in_a_row + 1 if "error" in record else 0
                        del pending_classifications[future]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("envs-cat-v2"))
    ap.add_argument("--work", type=Path, default=Path("pipeline-artifacts/envs-cat-v2"))
    ap.add_argument("--per-category", type=int, default=10)
    ap.add_argument("--classify-workers", type=int, default=4)
    ap.add_argument("--task-workers", type=int, default=_config.MAX_TASK_WORKERS)
    ap.add_argument("--qualification-workers", type=int, default=_config.MAX_QUALIFY_WORKERS)
    ap.add_argument("--native-workers", type=int, default=2)
    ap.add_argument("--native-concurrency", type=int, default=16,
                    help="shared cap on native episodes across all tasks and models")
    ap.add_argument('--local-workers', type=int, default=min(32, os.cpu_count() or 1),
                    help='separate Python processes for CPU-heavy local checks')
    ap.add_argument("--selected-only", action="store_true",
                    help="use the already classified eligible pool without additional screening")
    ap.add_argument('--eager-selected', action='store_true',
                    help='author all available selected sources while qualification runs')
    ap.add_argument("--gate-repairs", type=int, default=None,
                    help=f"targeted gate repair passes per candidate (default: policy "
                         f"MAX_REPAIR_PASSES = {_policy.MAX_REPAIR_PASSES}); 0 for one-shot")
    ap.add_argument("--provider-concurrency", type=int, default=_config.MAX_PROVIDER_CONCURRENCY)
    ap.add_argument("--review-provider-concurrency", type=int, default=_config.MAX_PROVIDER_CONCURRENCY)
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if min(args.per_category, args.classify_workers, args.task_workers,
           args.qualification_workers, args.native_workers, args.native_concurrency, args.local_workers, args.provider_concurrency,
           args.review_provider_concurrency) < 1:
        ap.error("Counts and concurrency must be positive")
    root = Path(__file__).resolve().parent
    if not args.worker:
        from pipeline.artifacts import bundle_lock
        work, out = args.work.resolve(), args.out.resolve()
        if out == work or out in work.parents or work in out.parents:
            ap.error("Output and artifact workspace must be separate")
        with bundle_lock(work):
            if args.gate_repairs is not None and args.gate_repairs < 0:
                ap.error("--gate-repairs must be nonnegative")
            request = snapshot(root, work, out, args.per_category, args.gate_repairs)
            if args.selected_only and not request.get('selected_only'):
                from pipeline.artifacts import atomic_json
                request['selected_only'] = True
                atomic_json(work / 'request.json', request)
            if args.eager_selected and not request.get('eager_selected'):
                if not request.get('selected_only'):
                    ap.error('--eager-selected requires --selected-only')
                from pipeline.artifacts import atomic_json
                request['eager_selected'] = True
                atomic_json(work / 'request.json', request)
            if args.plan_only:
                print(json.dumps({k: request[k] for k in ("requested", "source_capacity", "policy_sha256", "out")}, indent=2))
                return 0
            refresh_anthropic_credentials(root / "keys.env")
            env = dict(os.environ, ARENA_PIPELINE_ARTIFACTS=str(work),
                ARENA_GATE_REPAIRS=str(gate_repair_budget(request)),
                ARENA_DIFFICULTY_REPAIRS="0",
                ARENA_NATIVE_CONCURRENCY=str(args.native_concurrency),
                ARENA_LOCAL_WORKERS='1', ARENA_LOCAL_CONCURRENCY=str(args.local_workers),
                ARENA_PROVIDER_CONCURRENCY=str(args.provider_concurrency), ARENA_MAX_API_REQUESTS="20000",
                ARENA_REVIEW_PROVIDER_CONCURRENCY=str(args.review_provider_concurrency),
                ARENA_TRANSPORT_OBSERVATIONS=str(work / "transport.jsonl"),
                ARENA_CALL_DEADLINE_S="inf",
                ARENA_KEYS_FILE=str(root / "keys.env"),
                ARENA_HARBOR_BIN=str(root / ".venv-harbor/bin/harbor"),
                ARENA_RELEASE_REGISTRY=str(root / "pipeline-artifacts/releases"))
            command = [sys.executable, "-u", str(Path(request["code"]) / "category_batch.py"),
                "--worker", "--work", str(work), "--classify-workers", str(args.classify_workers),
                "--task-workers", str(args.task_workers),
                "--qualification-workers", str(args.qualification_workers),
                "--native-workers", str(args.native_workers),
                "--native-concurrency", str(args.native_concurrency),
                '--local-workers', str(args.local_workers),
                "--provider-concurrency", str(args.provider_concurrency),
                "--review-provider-concurrency", str(args.review_provider_concurrency)]
            return subprocess.call(command, cwd=request["code"], env=env)
    os.environ['ARENA_NATIVE_CONCURRENCY'] = str(args.native_concurrency)
    from pipeline import orchestrate
    orchestrate.DESIGN_ATTEMPTS = 1
    request = read(args.work / "request.json")
    configure_provider_admission(args.provider_concurrency, args.review_provider_concurrency,
                                 request.get("provider_budget_checkpoint"))
    controller = Controller(request, args.classify_workers, args.task_workers,
                            args.qualification_workers, args.native_workers)
    stop = threading.Event()

    def credentials():
        path = os.environ.get("ARENA_KEYS_FILE")
        if not path:
            return
        while not stop.is_set():
            try:
                refresh_anthropic_credentials(path)
            except (OSError, ValueError, RuntimeError):
                # An editor's incomplete write must not erase a working key.
                # Authentication errors stop new admission through the guard.
                pass
            if stop.wait(2):
                break

    def heartbeat():
        while not stop.wait(15):
            controller.save()

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    credential_thread = threading.Thread(target=credentials, daemon=True)
    credential_thread.start()
    try:
        return controller.run()
    except BaseException:
        controller.phase = "interrupted"
        controller.save()
        raise
    finally:
        stop.set()
        thread.join(timeout=1)
        credential_thread.join(timeout=1)


if __name__ == "__main__":
    raise SystemExit(main())
