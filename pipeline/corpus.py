"""A local release catalog: related tasks cannot silently become new releases.

Session identity and weighted exact source overlap are conservative family
signals. They do not prove semantic novelty; the independent critic does that.
"""
from __future__ import annotations
import os
from pathlib import Path
from .artifacts import atomic_json, bundle_lock, identity
from .author.contracts import strict_json


def registry_root() -> Path:
    return Path(os.environ.get("ARENA_RELEASE_REGISTRY", str(
        Path(__file__).resolve().parents[1] / "pipeline-artifacts/releases"))).resolve()


def signature(bundle: Path) -> dict:
    import hashlib
    spec = strict_json((bundle / "authoring/spec.json").read_text())
    source = spec.get("source", {})
    files = {}
    for path in (bundle / "environment/repo").rglob("*"):
        if path.is_symlink():
            raise ValueError("source family cannot contain symlinks")
        if path.is_file():
            raw = path.read_bytes()
            files[hashlib.sha256(raw).hexdigest()] = len(raw)
    return {"artifact_sha256": identity(bundle), "session_id": source.get("session_id"),
            "fixture_hashes": files, "bundle": str(bundle.resolve())}


def related(left: dict, right: dict) -> bool:
    if left.get("session_id") and left["session_id"] == right.get("session_id"):
        return True
    a, b = left["fixture_hashes"], right["fixture_hashes"]
    denominator = min(sum(a.values()), sum(b.values()))
    return bool(denominator and sum(a[h] for h in a.keys() & b.keys()) / denominator >= .8)


def problems(candidate: dict, registry: Path) -> list[str]:
    errors = []
    for path in registry.glob("*.json"):
        other = strict_json(path.read_text())
        if other.get("artifact_sha256") != candidate["artifact_sha256"] and related(candidate, other):
            errors.append(f"related task is already released: {path.stem}; retire/supersede it explicitly before publishing this version")
    return errors


def _lineage(bundle: Path) -> Path:
    """The lineage root of a bundle: `<lineage>/hardened/vN` belongs to `<lineage>`."""
    bundle = Path(bundle).resolve()
    return bundle.parent.parent if bundle.parent.name == "hardened" else bundle


def supersede(bundle: Path, reason: str, registry: Path | None = None) -> list[dict]:
    """Retire, explicitly and on the record, the catalog entries released from EARLIER versions of this bundle's own
    lineage, so this version can replace them (what `problems` asks for: "retire/supersede it explicitly").

    Only the same lineage is touched: an entry from any other bundle -- a related task of the same session included
    -- stays and still blocks. Each retired entry moves to `<registry>/retired/` carrying the reason, the successor
    and when (2026-10-21: every audit repair of a shipped task stopped at qualify on its own earlier release).
    """
    import time
    if not str(reason or "").strip():
        raise ValueError("a supersede needs a reason")
    root = registry or registry_root()
    lineage, candidate = _lineage(bundle), signature(bundle)
    retired = []
    with bundle_lock(root):
        for path in sorted(root.glob("*.json")):
            entry = strict_json(path.read_text())
            if entry.get("artifact_sha256") == candidate["artifact_sha256"] or not entry.get("bundle"):
                continue
            if _lineage(Path(entry["bundle"])) != lineage or not related(candidate, entry):
                continue
            record = {"reason": reason.strip(), "superseded_by": str(Path(bundle).resolve()),
                      "superseded_by_artifact": candidate["artifact_sha256"], "at": time.time()}
            atomic_json(root / "retired" / path.name, {**entry, "retired": record})
            path.unlink()
            retired.append({"artifact_sha256": entry["artifact_sha256"], "bundle": entry["bundle"], **record})
    return retired


def review_context(bundle: Path, workspace: Path | None = None) -> dict:
    """Retrieve source-related and textually similar peers for the critic."""
    import re
    from .config import bundle_paths
    workspace = workspace or Path(__file__).resolve().parents[1]
    candidate = signature(bundle)
    lineage_path = bundle / 'provenance/replacement.json'
    predecessors = set()
    if lineage_path.exists():
        lineage = strict_json(lineage_path.read_text())
        values = lineage.get('predecessors')
        if (lineage.get('schema') != 'arena-pipeline/replacement/1'
                or not isinstance(lineage.get('reason'), str) or not lineage['reason'].strip()
                or not isinstance(values, list) or not 1 <= len(values) <= 8
                or any(not isinstance(v, str) or not re.fullmatch(r'[0-9a-f]{64}', v) for v in values)
                or len(set(values)) != len(values)):
            raise ValueError('invalid version-bound replacement declaration')
        predecessors = set(values)
    spec = strict_json((bundle / "authoring/spec.json").read_text())
    source = spec.get("source") or {}
    def words(value):
        return set(re.findall(r"\w+", str(value).casefold()))
    vocabulary = words(spec.get("novelty", "") + " " + spec.get("instruction", ""))
    peers = []
    roots = [p for p in workspace.glob("envs*") if p.is_dir()]
    roots += [workspace / "reference", workspace / "reference/prior-delivery"]
    seen, seen_artifacts, found_predecessors = set(), set(), set()
    same_task_versions = 0
    for root in roots:
        candidates = set(bundle_paths(root)) | {p.parent for p in root.glob("*/task.toml")}
        for peer in sorted(candidates):
            if peer.resolve() == bundle.resolve() or not (peer / "authoring/spec.json").is_file():
                continue
            if peer.resolve() in seen:
                continue
            seen.add(peer.resolve())
            other = strict_json((peer / "authoring/spec.json").read_text())
            try:
                peer_signature = signature(peer)
            except ValueError:
                # A peer that cannot be identified -- a sibling between its spec and its build during concurrent
                # builds, an incomplete copy -- is not a published task; it must not fail this review ("cannot
                # identify an incomplete bundle" ended a non-coding review unverified, 2026-10-30).
                continue
            peer_sha = peer_signature['artifact_sha256']
            # A transferred or staged copy is still this candidate, not a
            # competing task. Deduplicate peers by frozen bytes as well.
            if peer_sha == candidate['artifact_sha256'] or peer_sha in seen_artifacts:
                continue
            seen_artifacts.add(peer_sha)
            relation = related(candidate, peer_signature)
            is_predecessor = peer_sha in predecessors
            other_source = other.get("source") or {}
            same_task = (peer.name == bundle.name
                         or (source.get("session_id") is not None
                             and other_source.get("session_id") == source.get("session_id")
                             and other_source.get("seed_index") == source.get("seed_index")))
            if same_task and not is_predecessor:
                # Another export of THIS task -- the live bundle seen from a
                # staged pre-review draft, a retained draft seen from a
                # resumed build, a retired attempt -- is not a competing
                # release. Shown as a peer it drew "duplicate task under
                # new-task metadata" findings no designer could repair
                # (streamlit pre-review 004, 2026-09-29). A declared
                # predecessor is still reported as one.
                same_task_versions += 1
                continue
            if is_predecessor:
                if not relation:
                    raise ValueError('declared predecessor belongs to an unrelated source family')
                found_predecessors.add(peer_sha)
            tokens = words(other.get("novelty", "") + " " + other.get("instruction", ""))
            similarity = len(tokens & vocabulary) / max(1, len(tokens | vocabulary))
            peers.append({"artifact_sha256": peer_sha, "name": peer.name,
                          "source_related": relation, "similarity": round(similarity, 4),
                          "relationship": "predecessor" if is_predecessor else "peer",
                          "novelty": other.get("novelty"),
                          "instructions": [p.read_text() for p in sorted((peer / "steps").glob("*/instruction.md"))],
                          "checks": other.get("checks", [])})
    if found_predecessors != predecessors:
        raise ValueError('declared predecessor is missing or changed; retain the exact baseline for review')
    peers.sort(key=lambda p: (-int(p['relationship'] == 'predecessor'),
                             -int(p["source_related"]), -p["similarity"],
                             p["artifact_sha256"], p["name"]))
    relevant = [p for p in peers if p["source_related"] or p["similarity"] >= .2]
    return {"method": "session/source overlap or lexical Jaccard >=0.2; critic must assess semantic novelty",
            "design_mode": "replacement" if predecessors else "new_task",
            "replacement_reason": lineage['reason'] if predecessors else None,
            "peers_considered": len(peers), "same_task_versions_skipped": same_task_versions,
            "peers": relevant[:8]}


def register(bundle: Path, release: dict, registry: Path | None = None) -> list[str]:
    """Serialize duplicate detection and registration across worker processes."""
    if release.get("release_ready") is not True or release.get("artifact_sha256") != identity(bundle):
        raise ValueError("only verified exact artifacts may enter the release catalog")
    root = registry or registry_root()
    candidate = signature(bundle)
    with bundle_lock(root):
        errors = problems(candidate, root)
        if not errors:
            atomic_json(root / (candidate["artifact_sha256"] + ".json"), {
                **candidate, "policy_sha256": release["policy_sha256"],
                "release_receipt": str(bundle / "verification/release.json")})
        return errors


def main(argv: list[str] | None = None) -> int:
    """python -m pipeline.corpus supersede <bundle> --reason "<why>": retire the lineage's earlier releases."""
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="pipeline.corpus")
    ap.add_argument("action", choices=("supersede",))
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--reason", required=True)
    ns = ap.parse_args(argv)
    print(json.dumps(supersede(ns.bundle, ns.reason), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
