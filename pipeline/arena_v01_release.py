"""Release qualification on the Arena v0.1 package (user, 2026-10-24: "use the 0.24 calibration as release
qualification").

A version with a release TARGET (verification/arena-v01.json: lineage, claimed faithfulness level, package version,
domain, prompt language) is qualified on the Arena v0.1 runtime package DERIVED from it, not on the pipeline bundle:
`release._qualify` freezes the version as before, derives the v0.1 package from that frozen copy, and runs the native
oracle / nop controls and the 10 x 2 calibration on it (Harbor as requirements-native.lock pins it, 0.24.0 here). The
same campaigns are then the v0.1 package's calibration and Oracle evidence (orchestration/arena_v01_ship.py
`release-finish`): nothing is measured twice and nothing is relabelled.

The derivation is the delivery chain itself, as a pure function of the version's identity surfaces and the target:
pipeline.deliver's copy, rename and level (at the CLAIMED level) -> pipeline.arena_format.export (the supplier layout
the delivered zips carry) -> pipeline.arena_v01.convert_tree. The agent-facing surface (environment/, the step
instructions) is the bundle's; the verifier adds Arena's rubric and reward-details beside the unchanged grade.py.

Identity. A campaign over a derived package is admissible as this version's evidence only when the package
REPRODUCES: `evidence.native_evidence` re-derives the package from the bundle with the recorded parameters and
requires the campaign's own package copy to have exactly that identity (and the recorded converter, source identity
and Arena task_tree_sha256 to agree). An arbitrary tree, a package derived from another version, or one the current
converter no longer produces is refused -- never accepted because it "looks converted".

Each derived package carries SIDECAR/ (outside artifacts.ROOTS, so never inside a campaign's package copy): the
derivation record and a frozen copy of its source, so a campaign's preflight (`benchmark.preflight.validate_task`,
wherever it runs) re-checks the source against the current generator and re-derives the package before it spends.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import tomllib
from pathlib import Path

SCHEMA = "arena-pipeline/arena-v01-release/1"
#: task_validation.mode of a campaign over a derived package.
MODE = "arena-v01-derived"
#: The release target, in the version's verification/ (outside its identity).
TARGET = "verification/arena-v01.json"
#: Inside a derived package; outside artifacts.ROOTS, so Harbor's package copy never carries it.
SIDECAR = ".arena-v01"
#: Bumped when the derivation's output changes on purpose. Reproduction, not this string, is what binds a campaign;
#: the string says which converter a record came from.
CONVERTER = "arena-v01-derivation/1"
PARAMS = ("lineage", "level", "version", "domain", "prompt_language")
FORMAT = "Arena v0.1"


def default_lineage(bundle: Path) -> str:
    """The lineage's working name (c-NNN-slug): a hardened version sits at <lineage>/hardened/vN."""
    path = Path(bundle).resolve()
    return path.parent.parent.name if path.parent.name == "hardened" else path.name


def target(bundle: Path) -> dict | None:
    """The release target's parameters, or None when this version releases on the pipeline bundle."""
    path = Path(bundle) / TARGET
    if not path.is_file():
        return None
    doc = json.loads(path.read_text())
    params = doc.get("params") if isinstance(doc, dict) else None
    if (doc.get("schema") != SCHEMA + "/target" or not isinstance(params, dict) or set(params) != set(PARAMS)
            or not all(isinstance(params[k], str) and params[k] for k in PARAMS)):
        raise ValueError(f"{path}: malformed Arena v0.1 release target")
    return dict(params)


#: Arena's checker pattern for [metadata] prompt_language (BCP 47) and domain (snake_case).
LANGUAGE_TAG = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
#: Scripts that name one language without asking (anything else -- Latin, Arabic, Cyrillic -- needs a reader).
_SCRIPT_LANGUAGE = (("\u3040", "\u30ff", "ja"), ("\uac00", "\ud7af", "ko"), ("\u0e00", "\u0e7f", "th"),
                    ("\u0590", "\u05ff", "he"), ("\u0370", "\u03ff", "el"), ("\u0980", "\u09ff", "bn"),
                    ("\u0b80", "\u0bff", "ta"), ("\u0c00", "\u0c7f", "te"))


def instruction_text(bundle: Path) -> str:
    return "\n\n".join(p.read_text(errors="replace") for p in sorted(Path(bundle).glob("steps/*/instruction.md")))


def detect_prompt_language(bundle: Path, ask=None) -> str:
    """The BCP 47 tag of the language the task's instructions are written in (the user's, by the authoring rules).

    The driver releases unattended (2026-10-30 audit: choosing domain and language was a human step), so `auto`
    asks a model once -- `ask(text) -> str`, by default one low-effort call -- and checks the answer against
    Arena's pattern; without an answer only an unambiguous script decides. Refuses rather than guesses."""
    text = instruction_text(bundle)
    if not text.strip():
        raise ValueError("no instruction to read the prompt language from")
    tag = None
    try:
        tag = (ask or _ask_language)(text[:6000])
    except Exception:                                # noqa: BLE001 -- the script check below decides alone
        tag = None
    tag = (tag or "").strip().strip(".`'\"")
    if tag and LANGUAGE_TAG.fullmatch(tag):
        return tag
    letters = [ch for ch in text if ch.isalpha()]
    for lo, hi, code in _SCRIPT_LANGUAGE:
        if letters and sum(lo <= ch <= hi for ch in letters) / len(letters) > 0.3:
            return code
    raise ValueError(f"cannot settle the prompt language (answer {tag!r}); pass prompt_language explicitly")


def _ask_language(text: str) -> str:
    from .select.classify import _call
    # 1024, not 32: Opus 5.5 thinks first, and at 32 tokens the reply was a thinking block alone, so every Portuguese
    # task was refused as "cannot settle the prompt language (answer '')" (2026-10-30; 50 tokens suffice).
    body = {"model": "claude-opus-5-5", "max_tokens": 1024, "output_config": {"effort": "low"},
            "messages": [{"role": "user", "content": (
                "Reply with ONLY the BCP 47 language tag (lowercase primary subtag, e.g. en, ru, pt-BR, zh-Hans) "
                "of the language the following task instruction is written in. Code, file names and quoted "
                "data do not count; the language of the prose does.\n\n<<<\n" + text + "\n>>>")}]}
    out = _call(body)
    return "".join(b.get("text", "") for b in out.get("content") or [] if b.get("type") == "text")


def domain_slug(text: str) -> str:
    """Arena's snake_case domain from a free-text topic ('html/js single-page app' -> 'html_js_single_page_app')."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    if not slug or not slug[0].isalnum():
        raise ValueError(f"no snake_case domain in {text!r}")
    return slug


def write_target(bundle: Path, *, domain: str, prompt_language: str, level: str | None = None,
                 version: str | None = None, lineage: str | None = None) -> dict:
    """Record the target. The level defaults to the one the version measures NOW (before its calibration, F1's
    empirical clause is unmeasured, so an F1 candidate must claim F1 explicitly); `level_problems` holds the claim
    to the measured level once the calibration exists. The version defaults to the next minor of [task].version."""
    from . import naming
    from .artifacts import atomic_json
    from .ledger import levels
    bundle = Path(bundle)
    if level is None:
        ledger = json.loads((bundle / "provenance/ledger.json").read_text())
        level = levels.assign_measured(bundle, ledger)["level"]
    level = str(level).upper()
    if level not in naming.LEVELS:
        raise ValueError(f"faithfulness level must be one of {naming.LEVELS}, not {level!r}")
    if version is None:
        version = naming.next_version(tomllib.loads((bundle / "task.toml").read_text())["task"].get("version")
                                      or "1.0.0")
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(version)):
        raise ValueError(f"not an X.Y.Z version: {version!r}")
    if prompt_language == "auto":
        prompt_language = detect_prompt_language(bundle)
    domain = domain_slug(domain)
    params = {"lineage": lineage or default_lineage(bundle), "level": level, "version": str(version),
              "domain": str(domain), "prompt_language": str(prompt_language)}
    if not all(params.values()):
        raise ValueError(f"every target parameter is required: {params}")
    atomic_json(bundle / TARGET, {"schema": SCHEMA + "/target", "params": params,
                                  "note": "qualified on the Arena v0.1 package derived from this version "
                                          "(pipeline/arena_v01_release.py); changing it re-runs the calibration"})
    return params


# ---------------------------------------------------------------------------------------------------- derivation

def supplier(bundle: Path, work: Path, params: dict, *, receipts: bool = False,
             calibration_reproduces_failure: bool | None = None) -> Path:
    """The supplier-layout package of `bundle` under work/supplier/<env-id>: pipeline.deliver's allowlisted copy,
    ledger level, rename and task.toml level (at the claimed level), then pipeline.arena_format.export. receipts=False
    copies the identity surfaces only (what the derivation is a function of); release-finish copies the delivered
    receipts too, and passes the measured reproduction so the ledger states the measured level."""
    from . import arena_format, naming, policy
    from .artifacts import inventory
    from .deliver import delivered_files
    from .ledger import faithfulness
    bundle, work = Path(bundle), Path(work)
    files, problems = delivered_files(bundle)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    if not receipts:
        surfaces = set(inventory(bundle))
        files = [f for f in files if f in surfaces]
    level = params["level"]
    dest = work / "delivered" / policy.DELIVERY_DIRS[level] / naming.short_name(level, params["lineage"])
    shutil.rmtree(dest, ignore_errors=True)
    for rel in files:
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundle / rel, out)
    source_toml = (bundle / "task.toml").read_bytes().decode("utf-8")
    ledger = faithfulness.with_level(dest, json.loads((bundle / "provenance/ledger.json").read_text()),
                                     calibration_reproduces_failure)
    (dest / "provenance/ledger.json").write_text(json.dumps(ledger, indent=2))
    renamed = naming.apply(dest, level)
    (dest / "task.toml").write_text(naming.delivered_toml(source_toml, renamed["name"], level), encoding="utf-8")
    return arena_format.export(dest, work / "supplier")


def _build(source: Path, out_dir: Path, params: dict, work: Path) -> dict:
    from . import arena_v01
    from .artifacts import freeze
    frozen = work / "frozen"
    freeze(Path(source), frozen)            # the identity surfaces only: the derivation is a function of them
    src = supplier(frozen, work, params)
    return arena_v01.convert_tree(src, out_dir, domain=params["domain"], prompt_language=params["prompt_language"],
                                  version=params["version"])


_REPRODUCED: dict[tuple[str, str], dict] = {}


def reproduce(source: Path, params: dict) -> dict:
    """What deriving `source` with `params` produces now: {env_id, version, package_identity, task_tree_sha256}.
    Cached per process by (source identity, params)."""
    from .artifacts import identity
    params = {k: params[k] for k in PARAMS}
    key = (identity(Path(source)), json.dumps(params, sort_keys=True))
    if key not in _REPRODUCED:
        with tempfile.TemporaryDirectory(prefix="arena-v01-reproduce-") as tmp:
            made = _build(source, Path(tmp) / "out", params, Path(tmp))
            _REPRODUCED[key] = {"env_id": made["env_id"], "version": made["version"],
                                "package_identity": identity(Path(made["package"])),
                                "task_tree_sha256": made["task_tree_sha256"]}
    return dict(_REPRODUCED[key])


def derive(source: Path, out_dir: Path, params: dict) -> dict:
    """The Arena v0.1 runtime package of the frozen version `source` at out_dir/<env-id>, with SIDECAR/derivation.json
    and SIDECAR/source (a frozen copy of `source`). Returns the derivation record plus `package`."""
    from .artifacts import atomic_json, freeze, identity
    params = {k: params[k] for k in PARAMS}
    sha = identity(Path(source))
    with tempfile.TemporaryDirectory(prefix="arena-v01-derive-") as tmp:
        made = _build(source, Path(out_dir), params, Path(tmp))
    package = Path(made["package"])
    record = {"schema": SCHEMA + "/derivation", "mode": MODE, "converter": CONVERTER,
              "source_artifact_sha256": sha, "params": params, "env_id": made["env_id"],
              "version": made["version"], "package_identity": identity(package),
              "task_tree_sha256": made["task_tree_sha256"]}
    freeze(Path(source), package / SIDECAR / "source")
    atomic_json(package / SIDECAR / "derivation.json", record)
    _REPRODUCED.setdefault((sha, json.dumps(params, sort_keys=True)),
                           {k: record[k] for k in ("env_id", "version", "package_identity", "task_tree_sha256")})
    return {**record, "package": str(package)}


def _agrees(record: dict, again: dict) -> bool:
    return all(record.get(k) == v for k, v in again.items())


def validation(package: Path) -> dict | None:
    """A campaign preflight's task_validation for a derived package, or None when `package` carries no derivation.
    Raises when the derivation does not hold: its source is not current-generator clean, or re-deriving the source
    with the recorded parameters does not give exactly this package."""
    from .artifacts import identity, policy_identity
    from .assurance import generated_problems
    package = Path(package)
    side = package / SIDECAR / "derivation.json"
    if not side.is_file():
        return None
    record = json.loads(side.read_text())
    if record.get("schema") != SCHEMA + "/derivation" or record.get("mode") != MODE \
            or record.get("converter") != CONVERTER:
        raise ValueError(f"derivation record {record.get('schema')}/{record.get('converter')} is not "
                         f"{SCHEMA}/derivation by {CONVERTER}")
    source = package / SIDECAR / "source"
    if identity(source) != record.get("source_artifact_sha256"):
        raise ValueError("the derivation's source copy differs from its recorded identity")
    errors = generated_problems(source)
    if errors:
        raise ValueError("the source version is inconsistent with the current generator: " + "; ".join(errors[:3]))
    again = reproduce(source, record["params"])
    if again["package_identity"] != identity(package) or not _agrees(record, again):
        raise ValueError("the package is not what its recorded derivation produces")
    return {"artifact_sha256": record["source_artifact_sha256"], "policy_sha256": policy_identity(),
            "mode": MODE, "release_qualified": False, "current_generator_verified": True,
            "derivation": {k: record[k] for k in ("converter", "source_artifact_sha256", "params", "env_id",
                                                  "version", "package_identity", "task_tree_sha256")},
            "note": f"{FORMAT} package derived from a current reproducible export (pipeline/arena_v01_release.py); "
                    "production acceptance requires complete release qualification."}


def snapshot_problems(bundle: Path, package: Path, task_validation: dict) -> list[str]:
    """`evidence.native_evidence` for a derived campaign: its package copy must be exactly what deriving THIS
    version with the recorded parameters produces now."""
    from .artifacts import identity
    d = task_validation.get("derivation") or {}
    try:
        again = reproduce(Path(bundle), d.get("params") or {})
        held = (d.get("converter") == CONVERTER and d.get("source_artifact_sha256") == identity(Path(bundle))
                and _agrees(d, again) and identity(Path(package)) == again["package_identity"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"the {FORMAT} derivation could not be reproduced ({type(exc).__name__}: {str(exc)[:200]})"]
    return [] if held else [f"native snapshot is not the {FORMAT} derivation of this task"]


# ---------------------------------------------------------------------------------------------------- release side

def campaign_params(campaign: Path) -> tuple[str | None, dict | None]:
    """(task_validation.mode, derivation params) of a campaign's report."""
    report = json.loads((Path(campaign) / "report.json").read_text())
    validation_ = report.get("task_validation") or {}
    return validation_.get("mode"), (validation_.get("derivation") or {}).get("params")


def target_problems(campaign, params: dict | None) -> list[str]:
    """Does this campaign measure what the version releases on? With a target: the derived package of exactly that
    target. Without one: the pipeline bundle itself. Either way, never a mix."""
    name = Path(str(campaign)).name
    try:
        mode, got = campaign_params(Path(str(campaign)))
    except (OSError, ValueError, TypeError, AttributeError):
        # Without a target nothing changes: native_evidence reports an unreadable campaign as it always has.
        return [f"{name}: report unreadable"] if params is not None else []
    if params is None:
        return [f"{name}: measured an {FORMAT} package, but this version has no {FORMAT} release target"] \
            if mode == MODE else []
    if mode != MODE:
        return [f"{name}: not measured on the {FORMAT} package of the release target (task mode {mode})"]
    if got != params:
        return [f"{name}: measured another {FORMAT} target {got}, not {params}"]
    return []


def level_problems(bundle: Path, params: dict) -> list[str]:
    """The claimed level against the level the version measures with its calibration (F1's empirical clause reads
    it). task.toml carries the claim, so a claim the evidence does not support is refused, never shipped."""
    from .ledger import levels
    ledger = json.loads((Path(bundle) / "provenance/ledger.json").read_text())
    measured = levels.assign_measured(Path(bundle), ledger)["level"]
    if measured != params["level"]:
        return [f"the {FORMAT} package claims {params['level']} but this version measures {measured}: write the "
                f"target at {measured} and release again (the level is inside the calibrated task tree)"]
    return []


def calibration_of_record(bundle: Path) -> dict:
    """What the release record states about its calibration: format, Harbor version(s), package, every campaign."""
    from .author.contracts import strict_json
    bundle = Path(bundle)
    controls = strict_json((bundle / "verification/native-controls.json").read_text())
    index = strict_json((bundle / "verification/native-calibrations.json").read_text())
    params = target(bundle)
    harbor, derivations = set(), set()
    campaigns = {}

    def read(path):
        report = json.loads((Path(path) / "report.json").read_text())
        harbor.add(((report.get("runtime") or {}).get("versions") or {}).get("harbor"))
        d = (report.get("task_validation") or {}).get("derivation")
        if d:
            derivations.add(json.dumps({k: d.get(k) for k in ("env_id", "version", "task_tree_sha256")},
                                       sort_keys=True))
        return report

    for role in ("oracle", "nop"):
        read(controls[role])
    for primary in index.get("campaigns") or []:
        report = read(primary)
        extra = list((index.get("topups") or {}).get(str(primary)) or [])
        for part in extra:
            read(part)
        campaigns[report.get("resolved_model") or report.get("model")] = [str(primary), *map(str, extra)]
    package = json.loads(derivations.pop()) if len(derivations) == 1 else None
    return {"format": FORMAT if params else "pipeline bundle", "harbor": sorted(h for h in harbor if h),
            "target": params, "package": package, "oracle": controls.get("oracle"), "nop": controls.get("nop"),
            "campaigns": campaigns}


# ---------------------------------------------------------------------------------------------------- packaging

RELEASE_RECORD = "verification/shipfix-release.json"


def package(bundle: Path, packages: Path) -> dict:
    """After `shipfix --release` made this version ready on its v0.1 target: the supplier-layout package (with the
    delivered receipts and the MEASURED level in its ledger) at packages/.source/<env-id>@<version>/<env-id>, and the
    v0.1 runtime package at packages/<env-id> -- refused unless its Arena task tree and its identity are exactly the
    ones the release calibrated. Run where the bundle and its campaigns are authoritative (the release's workspace)."""
    from . import arena_v01
    from .artifacts import identity
    from .ledger import levels
    bundle, packages = Path(bundle).resolve(), Path(packages)
    record = json.loads((bundle / RELEASE_RECORD).read_text())
    params = target(bundle)
    calibration = record.get("calibration") or {}
    if not record.get("ready"):
        raise ValueError(f"{bundle}: the release is not ready ({RELEASE_RECORD})")
    if not params or calibration.get("format") != FORMAT or calibration.get("target") != params:
        raise ValueError(f"{bundle}: the release did not qualify an {FORMAT} package for the current target")
    problems = level_problems(bundle, params)
    if problems:
        raise ValueError("; ".join(problems))
    again = reproduce(bundle, params)
    want = calibration.get("package") or {}
    if any(want.get(k) != again[k] for k in ("env_id", "version", "task_tree_sha256")):
        raise ValueError(f"the calibrated package {want} is not what this version derives now {again}")
    env, version = again["env_id"], again["version"]
    source_root = packages / ".source" / f"{env}@{version}"
    shutil.rmtree(source_root, ignore_errors=True)
    with tempfile.TemporaryDirectory(prefix="arena-v01-package-") as tmp:
        src = supplier(bundle, Path(tmp), params, receipts=True,
                       calibration_reproduces_failure=levels.calibration_reproduces_failure(bundle))
        shutil.copytree(src, source_root / env, symlinks=True)
    made = arena_v01.convert_tree(source_root / env, packages, domain=params["domain"],
                                  prompt_language=params["prompt_language"], version=params["version"])
    pkg = Path(made["package"])
    if made["task_tree_sha256"] != again["task_tree_sha256"] or identity(pkg) != again["package_identity"]:
        raise ValueError(f"{env}: the packaged runtime differs from the calibrated one")
    return {**made, "params": params, "bundle": str(bundle), "calibration": calibration}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("target", help="write the version's Arena v0.1 release target")
    t.add_argument("bundle", type=Path)
    t.add_argument("--domain", required=True)
    t.add_argument("--language", required=True, help="the instruction's language (BCP 47)")
    t.add_argument("--level")
    t.add_argument("--version")
    t.add_argument("--lineage")
    d = sub.add_parser("derive", help="derive the v0.1 package (no campaign; a dry check of the derivation)")
    d.add_argument("bundle", type=Path)
    d.add_argument("out", type=Path)
    p = sub.add_parser("package", help="the released version's supplier and v0.1 packages")
    p.add_argument("bundle", type=Path)
    p.add_argument("--packages", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.cmd == "target":
        out = write_target(a.bundle, domain=a.domain, prompt_language=a.language, level=a.level,
                           version=a.version, lineage=a.lineage)
    elif a.cmd == "derive":
        params = target(a.bundle)
        if not params:
            ap.error(f"{a.bundle} has no {TARGET}")
        out = derive(a.bundle, a.out, params)
    else:
        out = package(a.bundle, a.packages)
    print(json.dumps(out, indent=None if a.cmd == "package" else 1, default=str))  # package: one line, parsed
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
