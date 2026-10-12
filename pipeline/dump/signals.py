"""Join evaluation signals to task seeds, and lift the correction text.

The spec is explicit that the five signals are "individually noisy and
reward-hackable, but unbiased in aggregate -- use them to find promising
material and failure modes, not as the environment's reward." This module
treats them exactly that way: they locate moments, and nothing here scores
anything.

Two structural facts make the join exact, both verified on this dump:

  * every `tasks[].turn_ids` entry appears in some `messages[].node_ids`, so a
    signal resolves to a seed with no fuzzy matching; and
  * failures concentrate -- one 12-seed session put 21 of its 26 failures in 3
    seeds. That is why the seed span, not the session, is the unit of selection.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..config import DEAD_SIGNALS, sessions_root


@dataclass(frozen=True)
class Moment:
    """One failure signal, resolved back to the conversation around it."""
    session_id: str
    seed_index: int | None
    signal: str
    msg_index: int
    user_text: str          # the correction, in the user's words
    prior_assistant: str    # what it was a correction to
    language_hint: str = ""


@dataclass
class Seed:
    """One task seed: a turn span inside a session, the real unit of selection."""
    session_id: str
    index: int
    key: str
    n_turns: int
    lo: int = 0             # first message index
    hi: int = 0             # last message index
    signals: Counter = field(default_factory=Counter)
    moments: list[Moment] = field(default_factory=list)

    @property
    def corrections(self) -> int:
        """Failure signals that carry information. `bash_recovery_steps` is
        excluded: it measures recovery latency after a command failed, which is
        a property of the sandbox as much as the model."""
        return (self.signals["steerability|failure"]
                + self.signals["praise_complaint|failure"]
                + self.signals["task_outcome_explicit|failure"])

    @property
    def endorsements(self) -> int:
        return (self.signals["steerability|success"]
                + self.signals["task_outcome_explicit|success"])


def _text(msg: dict) -> str:
    return " ".join(c.get("text", "") for c in msg.get("content", [])
                    if c.get("type") == "text").strip()


def _language_hint(text: str) -> str:
    """Crude script detection. The dump's prompts are multilingual and a
    translation is included, but the script tells us whether a fixture lifted
    verbatim would carry non-English strings into the environment."""
    for lo, hi, name in ((0x0400, 0x04FF, "cyrillic"), (0x0600, 0x06FF, "arabic"),
                         (0x4E00, 0x9FFF, "han"), (0x3040, 0x30FF, "kana"),
                         (0xAC00, 0xD7AF, "hangul"), (0x0900, 0x097F, "devanagari"),
                         (0x0A80, 0x0AFF, "gujarati")):
        if any(lo <= ord(ch) <= hi for ch in text[:400]):
            return name
    return "latin"


def build_seeds(trace: dict, outcomes: dict) -> list[Seed]:
    messages = trace.get("messages", [])

    node_to_msg: dict[str, int] = {}
    for i, msg in enumerate(messages):
        for node in msg.get("node_ids", []):
            node_to_msg.setdefault(node, i)

    node_to_seed: dict[str, int] = {}
    seeds: dict[int, Seed] = {}
    for task in trace.get("tasks", []):
        idx = task["task_index"]
        spans = [node_to_msg[n] for n in task.get("turn_ids", []) if n in node_to_msg]
        seeds[idx] = Seed(
            session_id=trace.get("session_id", "?"), index=idx,
            key=task.get("task_key", ""), n_turns=task.get("n_turns", 0),
            lo=min(spans) if spans else 0, hi=max(spans) if spans else 0)
        for node in task.get("turn_ids", []):
            node_to_seed[node] = idx

    for row in outcomes.get("outcomes", []):
        signal = row.get("signal_name")
        if signal in DEAD_SIGNALS:
            continue
        node = row.get("annotating_node_id") or row.get("terminal_node_id")
        seed_index = node_to_seed.get(node)
        if seed_index is None or seed_index not in seeds:
            continue
        seed = seeds[seed_index]
        outcome = row.get("outcome")
        seed.signals[f"{signal}|{outcome}"] += 1
        if outcome != "failure":
            continue
        i = node_to_msg.get(node)
        if i is None:
            continue
        # The annotated node may be the assistant turn being corrected; the
        # correction itself is the next user turn.
        j = i
        while j < len(messages) and messages[j].get("role") != "user":
            j += 1
        user = _text(messages[j]) if j < len(messages) else ""
        prior = ""
        for k in range(min(i, len(messages) - 1), -1, -1):
            if messages[k].get("role") == "assistant":
                prior = _text(messages[k])
                break
        seed.moments.append(Moment(
            session_id=seed.session_id, seed_index=seed_index, signal=signal,
            msg_index=i, user_text=user[:1200], prior_assistant=prior[:600],
            language_hint=_language_hint(user)))

    return [seeds[k] for k in sorted(seeds)]


def load_outcomes(session_id: str, root: Path | None = None) -> dict:
    root = root or sessions_root()
    with open(root / session_id / "outcomes.json") as fh:
        return json.load(fh)


def persistence_runs(seed: Seed) -> list[int]:
    """Lengths of consecutive-turn correction runs inside the seed.

    A user re-reporting the same defect on three consecutive turns is the
    strongest difficulty evidence a trace can carry: it is a frontier model
    being told precisely what is wrong and failing again anyway. A single
    correction says only that the first attempt missed.
    """
    idx = sorted({m.msg_index for m in seed.moments})
    runs, current = [], 1
    for a, b in zip(idx, idx[1:]):
        if b - a <= 2:          # one assistant turn between two corrections
            current += 1
        else:
            runs.append(current)
            current = 1
    if idx:
        runs.append(current)
    return sorted(runs, reverse=True)
