"""Does the published instruction settle a hidden case? Ask both calibration families.

User, 2026-10-04: every failure must be a genuine capability failure. A hidden
case mined to beat a model can instead enforce ONE reading of a sentence that
admits two -- the commonest way "difficulty" turns out to be ambiguity
(PatchDiff: 66% of divergent plausible patches were uncertain because the
issue under-specified them; analysis/web-research/W6). Reference-and-alternative
agreement does not rule it out: independent implementations share misreadings.

The probe isolates the RULE from the TASK. Each calibration family is shown the
instruction exactly as the agent sees it plus one concrete input, and asked one
direct question with a short answer. The agent in the real task has to notice
the case, find the code, change it and keep everything else working; the probe
only asks what the rule says. When every family answers what the reference
computes, the instruction settles the case, and an agent that later fails it
inside the task failed to apply a rule it can read -- a capability failure.
When any family answers differently, the case is not admitted.

Advisory to the designer, recorded for the reviewer (`verification/determinacy/`).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

PROBE_MODELS = ("claude-fable-5-1", "gpt-6-astra")

PROMPT = """\
A user gave a developer the instruction below. Read it as the developer would, and answer \
one question about what the user's rules require for one concrete input. Do not write code. \
If the instruction does not settle the answer, say so.

INSTRUCTION
-----------
{instruction}

CONTEXT FROM THE REPOSITORY (verbatim, may be partial)
------------------------------------------------------
{context}

INPUT
-----
{case}

QUESTION
--------
{question}

Reply with your reasoning in at most five sentences, then a final line of exactly the form
ANSWER: <answer>
where <answer> is {answer_form}, or UNDETERMINED if the instruction does not settle it."""


def _normal(text: str) -> str:
    text = str(text or "").strip().strip(".").strip()
    text = re.sub(r"\s+", " ", text)
    return text.casefold()


def _answer(text: str) -> str | None:
    found = re.findall(r"^\s*ANSWER:\s*(.+?)\s*$", text or "", re.M)
    return found[-1] if found else None


def _ask(model: str, prompt: str) -> str:
    if model.startswith("gpt-"):
        from ..calibrate.agent_openai import _call
        response = _call({"model": model, "input": [{"role": "user", "content": prompt}],
                          "reasoning": {"effort": "high"}, "max_output_tokens": 4000})
        parts = []
        for item in response.get("output") or []:
            for block in item.get("content") or [] if isinstance(item, dict) else []:
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    parts.append(block.get("text") or "")
        return "\n".join(parts)
    from .transport import _call
    response = _call({"model": model, "max_tokens": 4000, "output_config": {"effort": "high"},
                      "messages": [{"role": "user", "content": prompt}]})
    return "\n".join(b.get("text", "") for b in response.get("content") or [] if b.get("type") == "text")


def probe(instruction: str, case: str, question: str, expected: str, *, context: str = "",
          answer_form: str = "a short answer", models: tuple[str, ...] = PROBE_MODELS,
          record_dir: Path | None = None, ask=None) -> dict:
    """Every family answers `question` about `case` from the instruction alone.

    `expected` is what the reference computes for the case. `determinate` is
    True only when every family's ANSWER equals it (case- and space-insensitive).
    """
    ask = ask or _ask
    prompt = PROMPT.format(instruction=instruction.strip(), context=(context or "(none)").strip()[:6000],
                           case=case.strip()[:6000], question=question.strip(), answer_form=answer_form)
    answers = {}
    for model in models:
        try:
            text = ask(model, prompt)
            got = _answer(text)
            answers[model] = {"answer": got, "agrees": got is not None and _normal(got) == _normal(expected),
                              "undetermined": got is not None and _normal(got) == "undetermined",
                              "reasoning": (text or "")[-1200:]}
        except Exception as exc:                          # noqa: BLE001 -- an unanswered probe admits nothing
            answers[model] = {"answer": None, "agrees": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    result = {"schema": "arena-coding/determinacy-probe/1", "question": question, "case": case[:2000],
              "expected": expected, "answer_form": answer_form, "answers": answers,
              "determinate": all(a.get("agrees") for a in answers.values()),
              "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "at": time.time()}
    if record_dir is not None:
        from ..artifacts import atomic_json
        atomic_json(Path(record_dir) / f"{result['prompt_sha256'][:16]}.json", result)
    return result
