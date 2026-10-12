"""Minimize a recognized exam-result capture without changing question content.

This policy removes a personal attempt header, question-level performance and
the student's responses. Published correct answers and question/options remain.
It deliberately rejects unknown header forms instead of deleting arbitrary prose.
"""
from __future__ import annotations

import re

QUESTION = re.compile(r"^(?P<heading>#{1,6}[ \t]+)?(?P<bold>\*\*)?Question[ \t]+(?P<item>\d+)\b(?P<tail>[^\r\n]*)", re.M)
ANSWER = re.compile(r"^[ \t]*(?:\*[ \t]+)?(?:\*\*)?Your Answer[ \t]*:(?:\*\*)?[^\r\n]*", re.M)
STATUS = r"(?:Correct|Incorrect|Unanswered|Not answered|Flagged|Flapped)"


def changes(content: str) -> list[dict]:
    questions = list(QUESTION.finditer(content))
    if not questions:
        raise ValueError("exam minimization needs recognizable question blocks")
    header = content[:questions[0].start()]
    if not re.search(r"\bExam Result\b", header):
        raise ValueError("exam minimization requires an exam-result header")
    titles = [line.strip().strip("*") for line in header.splitlines()
              if re.search(r"\bModel Set\b", line)]
    if len(titles) != 1:
        raise ValueError("exam minimization requires one unambiguous model-set title")
    newline = "\r\n" if "\r\n" in header else "\n"
    # Read only a total explicitly stated in the private header. Never infer
    # paper size from the number of captured questions or the student's score.
    totals = re.findall(r"Questions Answered\*{0,2}:?\*{0,2}\s*\d+\s+[Oo]ut of\s+(\d+)", header)
    if len(set(totals)) > 1:
        raise ValueError("exam-result header contains conflicting paper totals")
    replacement = "**Exam Result — personal attempt details withheld**" + newline + titles[0] + newline
    if totals:
        replacement += "Captured paper size: " + totals[0] + newline
    replacement += newline
    edits = [(0, questions[0].start(), replacement)]
    for match in questions:
        tail = re.sub(r"[\s*(),:/\d-]+", " ", match["tail"]).strip()
        # A pipe is metadata only between recognized status labels. Do not
        # erase a bare or numeric pipe expression that could be question text.
        if tail and not re.fullmatch(rf"{STATUS}(?:\s*\|?\s*{STATUS})*", tail, re.I):
            raise ValueError("unrecognized question heading; refusing to erase question text")
        prefix = match["heading"] or ""
        title = "Question " + match["item"]
        if match["bold"]:
            title = "**" + title + "**"
        if match.group() != prefix + title:
            edits.append((match.start(), match.end(), prefix + title))
    for match in ANSWER.finditer(content):
        end = match.end()
        value = re.sub(r"[\s*]", "", match.group().split(":", 1)[1])
        if not value:
            following = re.match(r"\r?\n([^\r\n]+)", content[end:])
            if (following is None or QUESTION.match(following[1])
                    or re.search(r"Correct\s+(?:Answer|response)", following[1], re.I)):
                raise ValueError("student response has no unambiguous single-line value")
            end += following.end()
        edits.append((match.start(), end, "[Student response withheld]"))
    return [{"start": start, "end": end, "replacement": replacement,
             "line": content.count("\n", 0, start) + 1,
             "kind": "exam-attempt-metadata"} for start, end, replacement in edits]
