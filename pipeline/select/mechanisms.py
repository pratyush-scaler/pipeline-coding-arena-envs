"""Evidence-grounded difficulty opportunities; advisory selection, never exclusion."""
from ..author.difficulty import MECHANISMS

#: A catalogue lever counts as supported by a seed's classification at this
#: relevance score (`author.levers.relevant`): two signals in the classifier's
#: own text, or one plus a correction class that grounds it. Measured on the 79
#: surviving seeds (2026-10-03): a single keyword qualified every seed for
#: every lever, which orders nothing.
LEVER_SUPPORT_SCORE = 5.0


def opportunities(classification: dict) -> list[dict]:
    inspected = {e for e in classification.get("evidence_inspected") or [] if isinstance(e, str)}
    result = []
    rows = classification.get("mechanism_opportunities")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("mechanism"), str) or row["mechanism"] not in MECHANISMS:
            continue
        evidence = row.get("evidence")
        if (not isinstance(evidence, list) or not evidence or
                any(not isinstance(e, str) or e not in inspected for e in evidence)):
            continue
        if not all(isinstance(row.get(k), str) and row[k].strip()
                   for k in ("tempting_rule", "divergence_case")):
            continue
        result.append(row)
    return result


def levers_supported(classification: dict) -> list[str]:
    """Catalogue levers the classification's own text and corrections ground.

    Deterministic and free: no model call, and the classifier's schema (and so
    its cache) is unchanged. The designer re-ranks with the fixture's code.
    """
    from ..author.levers import relevant
    from ..author.levers import LEVERS
    return [r["lever"] for r in relevant(classification, None, limit=len(LEVERS))
            if r["score"] >= LEVER_SUPPORT_SCORE]


def bonus(classification: dict, used=()) -> float:
    mechanisms = {r["mechanism"] for r in opportunities(classification)}
    levers = set(levers_supported(classification))
    used = set(used)
    # Capped ordering preference, never a measured difficulty score or a tier override.
    return (1 + .08 * len(mechanisms) + .04 * len(mechanisms - used)
            + .02 * min(6, len(levers)) + .01 * min(6, len(levers - used)))
