"""Why each shared session produced no environment, derived rather than typed.

Arena's batch acceptance requires 100% of shared traces dispositioned:
"converted -> env IDs, or excluded -> reason". `deliverable.trace_accounting`
derives the converted side from the delivered tree, which leaves the exclusions.

Typing those by hand is how a batch report drifts from what the funnel actually
did, so every reason here is recomputed from the selection artifacts the funnel
already wrote. The exclusions carry a reason *code* as well as prose because the
distinction the codes draw is the one a reader of this report most needs:

    kill_filter                 we looked and the session cannot support a task
    no_runtime                  the dominant language has no shipped toolchain
    no_source_fixture           no repository to lift (authored fixtures off)
    structural_assertion / requires_human_judgement / none
                                we looked and nothing an executed test can check
    qualified_not_built         it qualified; we ran out of authoring budget
    below_classification_depth  we never looked

Only the first two are judgements about Arena's corpus. The last two are
statements about ours, and collapsing them into "excluded" would report our
capacity as a property of their traces.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

#: Reason codes that describe our capacity, not the session's suitability.
OURS_NOT_THEIRS = ("qualified_not_built", "below_classification_depth")

#: We could not check this session. Not a disposition: `reasons` leaves it out,
#: so the batch report shows it as undispositioned instead of excluded.
UNASSESSED = "unassessed"

_HEADS = {
    "structural_assertion": "no executable truth source (structural_assertion: only file "
                            "presence or text patterns could be checked): ",
    "requires_human_judgement": "no executable truth source (requires_human_judgement: only a "
                                "human or LLM judge could score it): ",
    "none": "no deterministic truth source: ",
    "classifier_error": "",
}


def _load(artifacts: Path, name: str) -> dict:
    return json.loads((artifacts / name).read_text())


def _rows(blob):
    return blob["rows"] if isinstance(blob, dict) and "rows" in blob else blob


def _group(rows, key="session_id") -> dict[str, list]:
    out: dict[str, list] = {}
    for row in rows:
        out.setdefault(row[key], []).append(row)
    return out


def _index_errors(index) -> dict[str, str]:
    """Sessions the index names as unreadable, mapped to the read error."""
    rows = index.get("index_errors") if isinstance(index, dict) else None
    return {str(r.get("session_id")): str(r.get("error") or "unreadable")
            for r in rows or () if isinstance(r, dict) and r.get("session_id")}


def _classified_reason(cls: list[dict]) -> tuple[str, str]:
    """Reason code and prose for a session every classified seed rejected.

    A seed with no verdict, or a verdict the classifier could not produce, means
    the session was not fully judged -- unassessed, never a rejection.
    """
    unjudged = [r for r in cls if not isinstance(r.get("classification"), dict)
                or r["classification"].get("error")]
    if unjudged:
        return UNASSESSED, (f"NOT ASSESSED: {len(unjudged)} of {len(cls)} seed(s) have "
                            "no usable classification. Re-classify before relying on "
                            "this disposition.")
    whys, codes = [], []
    for record in cls:
        verdict = record.get("classification")
        if not isinstance(verdict, dict):
            continue
        if verdict.get("error"):
            codes.append("classifier_error")
            whys.append(f"classifier could not parse this seed: {verdict['error']}")
            continue
        codes.append(str(verdict.get("truth_source") or "not_gradable"))
        if verdict.get("why_not"):
            whys.append(verdict["why_not"])
    code = Counter(codes).most_common(1)[0][0] if codes else "not_gradable"
    head = _HEADS.get(code, f"no usable truth source ({code}): ")
    return code, head + (whys[0] if whys else
                         "the classifier found no checkable truth source in any "
                         "seed of this session")


def exclusions(sessions: list[str], artifacts: Path) -> dict[str, dict]:
    """Map every shared session to `{"code", "reason"}`.

    Covers converted sessions too: `trace_accounting` prefers its own converted
    set, so a session that shipped an environment is reported as converted
    whatever this says about it.
    """
    from .select.score import kill_filters

    artifacts = Path(artifacts)
    index = _load(artifacts, "index.json")
    by_session = _group(_rows(index))
    unreadable = _index_errors(index)
    # No classification yet means nothing was classified: every session then
    # falls to the structural floors or to below_classification_depth ("we
    # never looked"), never to a judgement about the trace.
    classified = (_group(_load(artifacts, "classified.json")["rows"])
                  if (artifacts / "classified.json").is_file() else {})

    out: dict[str, dict] = {}
    for session in sessions:
        seeds, cls = by_session.get(session, []), classified.get(session, [])

        if session in unreadable and not seeds:
            # We failed to read it. Saying "no seed was extracted" would be a
            # judgement about Arena's trace resting on our own read failure.
            out[session] = {
                "code": UNASSESSED,
                "reason": " ".join((
                    "NOT ASSESSED: the index could not read this session "
                    f"({unreadable[session]}). Repair the dump copy and re-index "
                    "before relying on this disposition.").split())[:800],
            }
            continue
        if not seeds:
            code = "no_seed_extracted"
            reason = ("no correction or seed span was extracted from this "
                      "session, so there is no failure moment to author from")
        elif cls:
            survivors = [c for c in cls
                         if isinstance(c.get("classification"), dict)
                         and c["classification"].get("survives")]
            if survivors:
                code = "qualified_not_built"
                reason = ("qualified: the classifier found a checkable truth "
                          f"source in {len(survivors)} of {len(cls)} classified "
                          "seed(s), but no environment was built from it within "
                          "this batch's authoring budget. A capacity exclusion, "
                          "not a judgement that the session is unusable -- it "
                          "remains a live candidate for a later batch.")
            elif len(cls) < len(seeds):
                code = "below_classification_depth"
                reason = (f"partly classified: {len(cls)} of {len(seeds)} seed(s) were "
                          "classified and rejected; the rest were never reached. A "
                          "budget exclusion, not a judgement about the session.")
            else:
                code, reason = _classified_reason(cls)
        else:
            try:
                verdicts = [kill_filters(seed) for seed in seeds]
            except (KeyError, TypeError) as exc:
                # An index row the scorer cannot read is not a session we may
                # quietly call excluded. Say we could not check it: "could not
                # check" must never render as "nothing wrong".
                out[session] = {
                    "code": UNASSESSED,
                    "reason": ("NOT ASSESSED: this session's index row could not "
                               f"be scored ({type(exc).__name__}: {exc}). Re-index "
                               "before relying on this disposition."),
                }
                continue
            if not any(v.kept for v in verdicts):
                # The code is the floor most of its seeds fell at, so a session
                # of Roblox Lua reads as no_runtime and not as a generic kill.
                code = Counter(v.code or "kill_filter" for v in verdicts).most_common(1)[0][0]
                first = next(v.reason for v in verdicts if not v.kept and (v.code or "kill_filter") == code)
                reason = (f"structurally unusable: every one of its {len(seeds)} "
                          f"seed(s) failed a hard selection floor -- {first}")
            else:
                code = "below_classification_depth"
                kept = sum(1 for v in verdicts if v.kept)
                reason = (f"not reached: {kept} of {len(seeds)} seed(s) passed the "
                          "structural floors but ranked below the depth the "
                          "classification budget reached in this batch. A budget "
                          "exclusion, not a judgement about the session.")

        out[session] = {"code": code,
                        "reason": " ".join(reason.split())[:800]}
    return out


def reasons(sessions: list[str], artifacts: Path) -> dict[str, str]:
    """`exclusions` flattened to the session -> prose map the report takes.

    Unassessed sessions are omitted, so they surface as undispositioned.
    """
    return {s: r["reason"] for s, r in exclusions(sessions, artifacts).items()
            if r["code"] != UNASSESSED}


def summary(sessions: list[str], artifacts: Path) -> dict:
    """Counts by reason code, and how much of the shortfall is ours."""
    derived = exclusions(sessions, artifacts)
    counts = Counter(r["code"] for r in derived.values())
    ours = sum(n for code, n in counts.items() if code in OURS_NOT_THEIRS)
    return {"sessions": len(sessions),
            "by_code": dict(counts.most_common()),
            "capacity_limited": ours,
            "capacity_limited_share": round(ours / len(sessions), 4) if sessions else None}
