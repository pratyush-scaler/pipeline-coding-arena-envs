"""Deterministic input generators. No expected answers or task acceptance rules."""
from __future__ import annotations

import itertools
import random


def intersections(dimensions: dict, *, limit: int | None = None, seed: int = 0) -> dict:
    """Retain boundary intersections and report uncovered pairs under a sample budget."""
    if not dimensions or any(not isinstance(k, str) or not isinstance(v, list) or not v
                             for k, v in dimensions.items()):
        raise ValueError("dimensions must name nonempty arrays of contract-permitted values")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be a positive integer or null")
    names = list(dimensions)
    sizes = [len(dimensions[k]) for k in names]
    count = 1
    for n in sizes:
        count *= n
    rng = random.Random(seed)
    selected = set()
    if limit is None or count <= limit:
        selected.update(itertools.product(*(range(n) for n in sizes)))
    else:
        # Cover pairs without first materializing the entire Cartesian product.
        for a, b in itertools.combinations(range(len(names)), 2):
            for x, y in itertools.product(range(sizes[a]), range(sizes[b])):
                row = [rng.randrange(n) for n in sizes]
                row[a], row[b] = x, y
                selected.add(tuple(row))
        selected = set(sorted(selected)[:limit])
        while len(selected) < min(limit, count):
            selected.add(tuple(rng.randrange(n) for n in sizes))
    rows = sorted(selected)
    missing = []
    for a, b in itertools.combinations(range(len(names)), 2):
        observed = {(r[a], r[b]) for r in rows}
        missing += [{"dimensions": [names[a], names[b]], "indices": [x, y]}
                    for x, y in itertools.product(range(sizes[a]), range(sizes[b])) if (x, y) not in observed]
    return {"advisory_only": True, "seed": seed, "total_combinations": count,
            "cases": [dict(zip(names, (dimensions[k][i] for k, i in zip(names, row)))) for row in rows],
            "uncovered_pairs": missing, "oracle": "derive observable expectations independently from the published contract"}


def sequences(actions: list[dict], *, length: int = 5, count: int = 128, seed: int = 0,
              prefixes: list[list[dict]] | None = None) -> dict:
    """Generate repeat/reorder/retry histories, retaining every supplied critical prefix."""
    if not actions or any(not isinstance(a, dict) or not isinstance(a.get("name"), str) for a in actions):
        raise ValueError("actions must be objects with names and optional contract-permitted arguments")
    if type(length) is not int or length < 1 or type(count) is not int or count < 1:
        raise ValueError("length and count must be positive integers")
    rng = random.Random(seed)
    histories = list(prefixes or [])
    if any(not isinstance(p, list) or any(not isinstance(a, dict) for a in p) for p in histories):
        raise ValueError("prefixes must be arrays of action objects")
    # Repeated operations expose idempotency; paired orders expose stale state.
    histories += [[a] * length for a in actions]
    histories += [[a, b] for a, b in itertools.product(actions, repeat=2)] if length >= 2 else []
    while len(histories) < count:
        histories.append([rng.choice(actions) for _ in range(rng.randint(1, length))])
    return {"advisory_only": True, "seed": seed, "cases": histories,
            "note": "critical prefixes and pair coverage are retained even when they exceed count; "
                    "use deterministic clocks, restart barriers and an independent rule model in the tests"}


def checkpoint_template() -> list[dict]:
    return [{"name": name, "design_goal": goal, "preserve_prior_behavior": True}
            for name, goal in (
                ("initial", "one genuine source-grounded behavior"),
                ("representations", "equivalent encodings and near-equivalent identities"),
                ("incremental", "out-of-order updates and interacting exceptions"),
                ("retractions", "correction, retry and cache invalidation"),
                ("persistence", "restart, rollback and compatibility"),
                ("integration", "a cross-module invariant over the whole history"))]
