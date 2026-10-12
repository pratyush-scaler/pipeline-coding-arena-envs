"""A synthetic coding task in the shape the corpus produces, for smoke tests.

A small Python package with a persistent defect (monthly totals grouped by
month only, so years merge -- the visible repro spans one year and passes; the
hidden inputs span two) chained with a spec misreading (refunds are made
positive, the instruction says they subtract), a restraint constraint (the CLI
file must stay byte-identical; no stray files), and the four artefacts every
task needs: a reference patch, an independently written alternative fix, and
a mutant per trap that fixes only what is visible.

Nothing here touches a trace; it exists so the contract can be executed end
to end without a model call.
"""
from __future__ import annotations

import json
from pathlib import Path

PARSE_PY = '''"""Transaction file parser.

A transaction line is `YYYY-MM-DD,amount,memo`. Amounts are decimal strings;
a refund is a negative amount. Malformed lines are skipped and counted.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass
class Transaction:
    date: str
    amount: Decimal
    memo: str


def parse_lines(lines):
    """Yield Transaction objects; return (transactions, error_count)."""
    out, errors = [], 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",", 2)
        if len(parts) != 3:
            errors += 1
            continue
        date, amount, memo = parts
        if len(date) != 10 or date[4] != "-" or date[7] != "-":
            errors += 1
            continue
        try:
            value = abs(Decimal(amount))
        except InvalidOperation:
            errors += 1
            continue
        out.append(Transaction(date, value, memo.strip()))
    return out, errors
'''

PARSE_FIXED = PARSE_PY.replace("            value = abs(Decimal(amount))\n", "            value = Decimal(amount)\n")

REPORT_PY = '''"""Monthly reporting over parsed transactions."""
from decimal import Decimal


def monthly_totals(transactions):
    """Total amount per calendar month, keyed `YYYY-MM`, in chronological order."""
    totals = {}
    for t in transactions:
        key = t.date.split("-")[1]
        totals[key] = totals.get(key, Decimal("0")) + t.amount
    return {k: totals[k] for k in sorted(totals)}
'''

REPORT_FIXED = REPORT_PY.replace('        key = t.date.split("-")[1]\n', '        key = t.date[:7]\n')
# Grouping fixed, but the sort kept the old month-only idea: chronological
# order across years is wrong while every per-month total is right.
REPORT_MONTH_SORT = REPORT_FIXED.replace(
    "    return {k: totals[k] for k in sorted(totals)}\n",
    '    return {k: totals[k] for k in sorted(totals, key=lambda k: k.split("-")[1])}\n')

REPORT_ALT = '''"""Monthly reporting over parsed transactions."""
from collections import defaultdict
from datetime import date as _date
from decimal import Decimal


def monthly_totals(transactions):
    """Total amount per calendar month, keyed `YYYY-MM`, in chronological order."""
    buckets = defaultdict(lambda: Decimal("0"))
    for t in transactions:
        y, m, d = (int(x) for x in t.date.split("-"))
        stamp = _date(y, m, d)
        buckets[(stamp.year, stamp.month)] += t.amount
    ordered = sorted(buckets)
    return {f"{y:04d}-{m:02d}": buckets[(y, m)] for y, m in ordered}
'''


# ---- step 2: new information (a per-month count), applied on top of step 1
COUNTS_FN = """

def monthly_counts(transactions):
    \"\"\"Number of transactions per calendar month, keyed `YYYY-MM`, in chronological order.\"\"\"
    counts = {}
    for t in transactions:
        key = t.date[:7]
        counts[key] = counts.get(key, 0) + 1
    return {k: counts[k] for k in sorted(counts)}
"""

COUNTS_ALT = """

def monthly_counts(transactions):
    \"\"\"Number of transactions per calendar month, keyed `YYYY-MM`, in chronological order.\"\"\"
    from collections import Counter
    tally = Counter((int(t.date[:4]), int(t.date[5:7])) for t in transactions)
    return {f"{y:04d}-{m:02d}": tally[(y, m)] for y, m in sorted(tally)}
"""

# A refund "is not really a transaction" -- the misreading step 2 plants.
COUNTS_SKIP_REFUNDS = COUNTS_FN.replace("    for t in transactions:\n        key",
                                        "    for t in transactions:\n        if t.amount < 0:\n            continue\n        key")
# Keys in insertion order: right on every in-order file, wrong on a shuffled one.
COUNTS_UNORDERED = COUNTS_FN.replace("    return {k: counts[k] for k in sorted(counts)}", "    return counts")

INSTRUCTION_2 = """one more thing from finance, they also want how many transactions each month
had, next to the totals. add `monthly_counts(transactions)` in report.py, same
`YYYY-MM` keys as the totals and in the same order. a refund is a transaction and
counts, a malformed line is not a transaction and doesn't. still don't touch
cli.py, that's not ours, and don't add files, it goes in report.py.
"""

TEST_COUNTS = '''"""Hidden tests for step 2: per-month transaction counts."""
import os
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
REPO = Path(os.environ["ARENA_REPO"])
sys.path.insert(0, str(REPO))

from ledger.parse import parse_lines  # noqa: E402
from ledger.report import monthly_counts, monthly_totals  # noqa: E402


def _load(name):
    with (DATA / name).open(encoding="utf-8") as handle:
        return parse_lines(handle)


def test_refund_counted():
    transactions, _ = _load("refunds.csv")
    assert monthly_counts(transactions) == {"2024-05": 2, "2024-06": 2}


def test_counts_order_matches_totals():
    transactions, _ = _load("shuffled.csv")
    counts = monthly_counts(transactions)
    assert list(counts) == list(monthly_totals(transactions)) == ["2022-11", "2023-03", "2023-11", "2024-01"]
    assert counts == {"2022-11": 1, "2023-03": 1, "2023-11": 1, "2024-01": 1}


def test_malformed_not_counted():
    transactions, errors = _load("malformed.csv")
    assert monthly_counts(transactions) == {"2024-07": 1}
    assert errors == 3


def test_counts_two_years_separate():
    transactions, _ = _load("two_years.csv")
    assert monthly_counts(transactions) == {"2023-01": 1, "2023-02": 1, "2024-01": 1, "2024-02": 1}
'''

CLI_PY = '''"""ledger CLI: `python -m ledger.cli FILE` prints monthly totals as JSON."""
import json
import sys

from ledger.parse import parse_lines
from ledger.report import monthly_totals


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: ledger.cli FILE", file=sys.stderr)
        return 2
    with open(argv[0], encoding="utf-8") as handle:
        transactions, errors = parse_lines(handle)
    totals = monthly_totals(transactions)
    print(json.dumps({"months": {k: str(v) for k, v in totals.items()}, "errors": errors}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

SAMPLE = """# visible repro: one year, no refunds
2024-01-05,120.00,rent
2024-01-20,35.50,groceries
2024-02-02,120.00,rent
2024-03-15,12.00,coffee
"""

README = """# ledger

Monthly totals from a transactions CSV. `python -m ledger.cli sample.csv`.

Reported by users: February shows the wrong total when the file covers more
than one year. Refunds should reduce the month they land in.
"""

INSTRUCTION = """our monthly totals are wrong again and I need this fixed before the close.

two things people keep reporting. when a transactions file covers more than one
year the months get merged, jan 2023 and jan 2024 end up in one bucket. The
report must key each month as `YYYY-MM` and list months in chronological
order. and refunds (negative amounts) are being added instead of subtracted --
a refund must reduce the total of the month it lands in, and a month whose
refunds exceed its charges must show a negative total.

don't break what works. Malformed lines are skipped and counted; the count is
the `errors` field the CLI prints. `ledger/cli.py` is shared with another team.
Do not modify it, the fix goes in the library modules it imports.
`monthly_totals` keeps its name and signature; other code imports it. Do not
add files to the repository beyond the modules that already exist.

repo is at /app/repo. `sample.csv` reproduces the current output but it only
covers one year so it won't show you the first bug on its own.
"""

TEST_REPORT = '''"""Hidden behaviour tests for ledger. Inputs live beside this file."""
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parent / "data"
REPO = Path(os.environ["ARENA_REPO"])
sys.path.insert(0, str(REPO))

from ledger.parse import parse_lines  # noqa: E402
from ledger.report import monthly_totals  # noqa: E402


def _load(name):
    with (DATA / name).open(encoding="utf-8") as handle:
        return parse_lines(handle)


def test_two_years_do_not_merge():
    transactions, _ = _load("two_years.csv")
    totals = monthly_totals(transactions)
    assert list(totals) == ["2023-01", "2023-02", "2024-01", "2024-02"]
    assert totals["2023-01"] == Decimal("100.00")
    assert totals["2024-01"] == Decimal("50.00")


def test_visible_sample_unchanged():
    with (REPO / "sample.csv").open(encoding="utf-8") as handle:
        transactions, errors = parse_lines(handle)
    totals = monthly_totals(transactions)
    assert errors == 0
    assert totals == {"2024-01": Decimal("155.50"), "2024-02": Decimal("120.00"),
                      "2024-03": Decimal("12.00")}


def test_refund_reduces_its_month():
    transactions, _ = _load("refunds.csv")
    totals = monthly_totals(transactions)
    assert totals["2024-05"] == Decimal("60.00")


def test_refund_can_make_month_negative():
    transactions, _ = _load("refunds.csv")
    totals = monthly_totals(transactions)
    assert totals["2024-06"] == Decimal("-25.00")


def test_refunds_across_years_stay_separate():
    transactions, _ = _load("refunds_two_years.csv")
    totals = monthly_totals(transactions)
    assert totals["2023-12"] == Decimal("-10.00")
    assert totals["2024-12"] == Decimal("40.00")


def test_empty_file():
    transactions, errors = parse_lines([])
    assert list(transactions) == [] and errors == 0
    assert monthly_totals(transactions) == {}


def test_malformed_lines_counted():
    transactions, errors = _load("malformed.csv")
    assert errors == 3
    assert monthly_totals(transactions) == {"2024-07": Decimal("10.00")}


def test_chronological_order_across_years():
    transactions, _ = _load("shuffled.csv")
    assert list(monthly_totals(transactions)) == ["2022-11", "2023-03", "2023-11", "2024-01"]


def test_cli_json_shape():
    proc = subprocess.run([sys.executable, "-m", "ledger.cli", str(DATA / "two_years.csv")],
                          cwd=str(REPO), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert set(doc) == {"months", "errors"}
    assert Decimal(str(doc["months"]["2023-01"])) == Decimal("100.00")


def test_api_name_kept():
    import ledger.report as report
    assert callable(getattr(report, "monthly_totals", None))
'''

DATA = {
    "two_years.csv": "2023-01-10,100.00,a\n2023-02-10,20.00,b\n2024-01-10,50.00,c\n2024-02-10,5.00,d\n",
    "refunds.csv": "2024-05-01,100.00,order\n2024-05-09,-40.00,refund\n2024-06-01,10.00,x\n2024-06-02,-35.00,chargeback\n",
    "refunds_two_years.csv": "2023-12-01,5.00,a\n2023-12-05,-15.00,refund\n2024-12-01,40.00,b\n",
    "malformed.csv": "2024-07-01,10.00,ok\nnot,a\n2024-7-1,3.00,bad date\n2024-07-02,abc,bad amount\n",
    "shuffled.csv": "2024-01-01,1.00,a\n2022-11-01,1.00,b\n2023-11-01,1.00,c\n2023-03-01,1.00,d\n",
}


def _diff(path: str, before: str, after: str) -> str:
    import difflib
    lines = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                 fromfile="a/" + path, tofile="b/" + path)
    return "".join(lines)


def repo_files() -> dict[str, str]:
    return {"ledger/__init__.py": "", "ledger/parse.py": PARSE_PY, "ledger/report.py": REPORT_PY,
            "ledger/cli.py": CLI_PY, "sample.csv": SAMPLE, "README.md": README}


def spec(two_steps: bool = False) -> dict:
    fix = _diff("ledger/report.py", REPORT_PY, REPORT_FIXED) + _diff("ledger/parse.py", PARSE_PY, PARSE_FIXED)
    alt = _diff("ledger/report.py", REPORT_PY, REPORT_ALT) + _diff("ledger/parse.py", PARSE_PY, PARSE_FIXED)
    symptom_only = _diff("ledger/report.py", REPORT_PY, REPORT_FIXED)          # years fixed, refunds not
    month_sort = (_diff("ledger/report.py", REPORT_PY, REPORT_MONTH_SORT)       # both fixed, sort still by month
                  + _diff("ledger/parse.py", PARSE_PY, PARSE_FIXED))
    scope_creep = fix + _diff("ledger/cli.py", CLI_PY, CLI_PY.replace('print("usage: ledger.cli FILE", file=sys.stderr)',
                                                                        'print("usage: python -m ledger.cli FILE", file=sys.stderr)'))
    step = "01-fix"
    Q_KEY = "The report must key each month as `YYYY-MM` and list months in chronological order."
    Q_REFUND = ("a refund must reduce the total of the month it lands in, and a month whose refunds "
                "exceed its charges must show a negative total.")
    Q_ERRORS = "Malformed lines are skipped and counted; the count is the `errors` field the CLI prints."
    Q_CLI = "`ledger/cli.py` is shared with another team. Do not modify it"
    Q_NAME = "`monthly_totals` keeps its name and signature; other code imports it."
    Q_FILES = "Do not add files to the repository beyond the modules that already exist."
    Q_SAMPLE = "`sample.csv` reproduces the current output but it only covers one year"
    py = lambda name, w, band, basis, quote, trap=None, req=(): {  # noqa: E731
        "id": name, "kind": "objective", "weight": w, "band": band, "step": step, "basis": basis,
        "rule_quote": [quote], "weight_basis": "hidden behaviour the instruction states, weighted by the "
                                                "decision it takes to get right", "trap": trap, "requires": list(req),
        "runner": {"type": "pytest", "select": [f"test_report.py::{name.replace('o_', 'test_', 1)}"], "timeout_s": 60}}
    checks = [
        py("o_two_years_do_not_merge", 20, "core", "months are keyed YYYY-MM so years never merge", Q_KEY, "t_symptom_patch"),
        py("o_visible_sample_unchanged", 5, "floor", "the one-year sample keeps its totals and zero errors", Q_SAMPLE),
        py("o_refund_reduces_its_month", 15, "core", "a refund reduces the total of the month it lands in", Q_REFUND, "t_refund_sign"),
        py("o_refund_can_make_month_negative", 15, "core", "a month whose refunds exceed charges shows a negative total", Q_REFUND, "t_refund_sign"),
        py("o_refunds_across_years_stay_separate", 15, "core", "refunds and years together: both rules at once", Q_REFUND, "t_refund_sign"),
        py("o_malformed_lines_counted", 5, "floor", "malformed lines are skipped and counted as errors", Q_ERRORS),
        py("o_chronological_order_across_years", 15, "core", "months are listed in chronological order across years", Q_KEY, "t_symptom_patch"),
        py("o_cli_json_shape", 5, "floor", "the CLI prints JSON with months and errors", Q_ERRORS),
        {"id": "g_empty_file_unchanged", "kind": "guardrail", "weight": 0, "band": "floor", "step": step,
         "basis": "an empty file still yields no months and zero errors (existing behaviour preserved)",
         "rule_quote": [Q_ERRORS],
         "runner": {"type": "pytest", "select": ["test_report.py::test_empty_file"], "timeout_s": 60}},
        {"id": "g_cli_untouched", "kind": "guardrail", "weight": 0, "band": "floor", "step": step,
         "basis": "ledger/cli.py is shared with another team and must not be modified", "trap": "t_scope",
         "rule_quote": [Q_CLI], "runner": {"type": "hash-guard", "paths": ["ledger/cli.py"]}},
        {"id": "g_api_name_kept", "kind": "guardrail", "weight": 0, "band": "floor", "step": step,
         "basis": "monthly_totals keeps its name and signature", "rule_quote": [Q_NAME],
         "runner": {"type": "pytest", "select": ["test_report.py::test_api_name_kept"], "timeout_s": 60}},
        {"id": "g_no_shadow", "kind": "guardrail", "weight": 0, "band": "floor", "step": step,
         "basis": "a planted top-level ledger module or a test hook must never be graded instead of the real one",
         "rule_quote": [Q_FILES],
         "runner": {"type": "shadow-guard", "modules": ["ledger"]}},
        {"id": "g_no_new_files", "kind": "guardrail", "weight": 0, "band": "floor", "step": step,
         "basis": "no files are added to the repository beyond the existing modules", "trap": "t_scope",
         "rule_quote": [Q_FILES],
         "runner": {"type": "tree-guard", "allow": ["**/__pycache__/**", "*.pyc"], "max_new_files": 0}},
    ]
    step2 = "02-counts"
    Q2_COUNT = "a refund is a transaction and counts, a malformed line is not a transaction and doesn't."
    Q2_NAME = "add `monthly_counts(transactions)` in report.py, same `YYYY-MM` keys as the totals and in the same order."
    Q2_CLI = "still don't touch cli.py"
    Q2_FILES = "don't add files, it goes in report.py."
    py2 = lambda name, w, band, basis, quote, trap=None: {  # noqa: E731
        "id": name, "kind": "objective", "weight": w, "band": band, "step": step2, "basis": basis,
        "rule_quote": [quote], "weight_basis": "hidden behaviour the second instruction states", "trap": trap,
        "runner": {"type": "pytest", "select": [f"test_counts.py::{name.replace('o2_', 'test_', 1)}"], "timeout_s": 60}}
    checks2 = [
        py2("o2_refund_counted", 20, "core", "a refund is a transaction and is counted", Q2_COUNT, "t_step2_refund_counted"),
        py2("o2_counts_order_matches_totals", 12, "core", "count keys follow the totals' chronological order across years",
            Q2_NAME, "t_step2_order"),
        py2("o2_malformed_not_counted", 6, "floor", "a malformed line is not a transaction and is not counted",
            Q2_COUNT),
        py2("o2_counts_two_years_separate", 6, "floor", "count keys are YYYY-MM, so the same month of two years "
            "stays separate", Q2_NAME),
        {"id": "g2_cli_untouched", "kind": "guardrail", "weight": 0, "band": "floor", "step": step2,
         "basis": "ledger/cli.py is still shared with another team and must not be modified",
         "rule_quote": [Q2_CLI], "runner": {"type": "hash-guard", "paths": ["ledger/cli.py"]}},
        {"id": "g2_no_shadow", "kind": "guardrail", "weight": 0, "band": "floor", "step": step2,
         "basis": "the same shadow protection in step 2",
         "rule_quote": [Q2_FILES],
         "runner": {"type": "shadow-guard", "modules": ["ledger"]}},
        {"id": "g2_no_new_files", "kind": "guardrail", "weight": 0, "band": "floor", "step": step2,
         "basis": "no files are added in step 2 either; the count lives in report.py",
         "rule_quote": [Q2_FILES],
         "runner": {"type": "tree-guard", "allow": ["**/__pycache__/**", "*.pyc"], "max_new_files": 0}},
    ]
    fix2 = _diff("ledger/report.py", REPORT_FIXED, REPORT_FIXED + COUNTS_FN)
    alt2 = _diff("ledger/report.py", REPORT_ALT, REPORT_ALT + COUNTS_ALT)
    skip_refunds = _diff("ledger/report.py", REPORT_FIXED, REPORT_FIXED + COUNTS_SKIP_REFUNDS)
    unordered = _diff("ledger/report.py", REPORT_FIXED, REPORT_FIXED + COUNTS_UNORDERED)
    traps2 = [
        {"id": "t_step2_refund_counted", "type": "spec_misread", "surface": "ledger/report.py", "provenance": "authored",
         "grounding": "step 1 made refunds negative amounts; a count that reuses the totals' filter drops them",
         "grounded_in": "ledger/report.py", "checks": ["o2_refund_counted"], "mutant": "m_step2_skip_refunds",
         "beatable_by": "counting every parsed transaction whatever its sign"},
        {"id": "t_step2_order", "type": "spec_misread", "surface": "ledger/report.py", "provenance": "authored",
         "grounding": "every visible file is already in date order, so insertion order looks chronological",
         "grounded_in": "sample.csv", "checks": ["o2_counts_order_matches_totals"], "mutant": "m_step2_unordered",
         "beatable_by": "sorting the keys as the totals do"},
    ]
    mutants2 = [
        {"id": "m_step2_skip_refunds", "trap": "t_step2_refund_counted", "patch": skip_refunds,
         "reason": "counts only positive amounts, treating a refund as not a transaction", "fails": ["o2_refund_counted"]},
        {"id": "m_step2_unordered", "trap": "t_step2_order", "patch": unordered,
         "reason": "counts in insertion order, which every in-order file hides", "fails": ["o2_counts_order_matches_totals"]},
    ]
    if two_steps:
        checks = checks + checks2
    return {
        "name": "synthetic-ledger-totals", "description": "Fix cross-year month grouping and refund signs in a ledger library",
        "keywords": ["synthetic", "python", "debugging"], "language": "python",
        "toolchain": {"name": "python", "limits": {"verifier_timeout_s": 600, "cpus": 2, "memory_mb": 2048}},
        "difficulty": "The visible sample only shows one year; the month-only grouping passes it. Refunds are "
                      "silently made positive one module away from the reported symptom.",
        "novelty": "Two defects in two modules, one only visible on inputs the agent does not have.",
        "intent": "code debugging", "target_level": "F1",
        "source": {"session_id": "synthetic", "seed_index": 0, "tier": 4},
        "steps": [{"name": step, "instruction": INSTRUCTION, **({"trace_turn": 3} if two_steps else {})}]
                 + ([{"name": step2, "instruction": INSTRUCTION_2, "trace_turn": 7,
                      "new_information": "finance now also wants a per-month transaction count beside the totals"}]
                    if two_steps else []),
        "deliverables": [], "deps": {"requirements.txt": ""},
        "checks": checks,
        "suites": {"test_report.py": TEST_REPORT, **{f"data/{k}": v for k, v in DATA.items()},
                   **({"test_counts.py": TEST_COUNTS} if two_steps else {})},
        "solution": {step: {"patch": fix, "commands": [], "provenance": {"origin": "authored", "note": "synthetic"}},
                     **({step2: {"patch": fix2, "commands": [], "provenance": {"origin": "authored", "note": "synthetic"}}}
                        if two_steps else {})},
        "usable_corrections": 0,
        "alternative": {step: {"patch": alt}, **({step2: {"patch": alt2}} if two_steps else {})},
        "traps": (traps2 if two_steps else []) + [
            {"id": "t_symptom_patch", "type": "symptom_patch", "surface": "ledger/report.py", "provenance": "authored",
             "grounding": "the visible sample spans one year, so a month-keyed sort reproduces its order",
             "grounded_in": "sample.csv", "checks": ["o_two_years_do_not_merge", "o_chronological_order_across_years"],
             "mutant": "m_month_sort", "beatable_by": "ordering by the full YYYY-MM key, not the month"},
            {"id": "t_refund_sign", "type": "spec_misread", "surface": "ledger/parse.py", "provenance": "authored",
             "grounding": "abs() sits in the parser, one module away from the reported report symptom",
             "grounded_in": "ledger/parse.py", "checks": ["o_refund_reduces_its_month", "o_refund_can_make_month_negative",
                                                          "o_refunds_across_years_stay_separate"],
             "mutant": "m_symptom_only", "beatable_by": "following the amount from the parser"},
            {"id": "t_scope", "type": "constraint", "surface": "ledger/cli.py", "provenance": "authored",
             "grounding": "the CLI has an obvious usage-string nit an over-eager fix would touch",
             "grounded_in": "ledger/cli.py", "checks": ["g_cli_untouched", "g_no_new_files"], "mutant": "m_scope_creep",
             "beatable_by": "leaving cli.py alone"},
        ],
        "mutants": (mutants2 if two_steps else []) + [
            {"id": "m_symptom_only", "trap": "t_refund_sign", "patch": symptom_only,
             "reason": "fixes the reported year merge, leaves abs() in the parser",
             "fails": ["o_refund_reduces_its_month", "o_refund_can_make_month_negative", "o_refunds_across_years_stay_separate"]},
            {"id": "m_month_sort", "trap": "t_symptom_patch", "patch": month_sort,
             "reason": "both defects fixed, but months are still ordered by month number, so years interleave",
             "fails": ["o_two_years_do_not_merge", "o_chronological_order_across_years"]},
            {"id": "m_scope_creep", "trap": "t_scope", "patch": scope_creep,
             "reason": "the complete fix plus a helpful edit to the protected CLI", "fails": ["g_cli_untouched"]},
        ],
        "quality": {
            "axes": ["inference", "restraint"], "levers": ["join", "restraint"],
            "untested_surfaces": ["locale-dependent decimal parsing", "files with Windows line endings",
                                  "amounts with more than two decimal places"],
            "decisions": [
                {"id": "d_grouping", "question": "What is the grouping key, and what order do months come in when the file spans years?",
                 "checks": ["o_two_years_do_not_merge", "o_chronological_order_across_years"],
                 "sources": ["ledger/report.py", "sample.csv"],
                 "shortcut": "key on the month token because the sample only has one year"},
                {"id": "d_refunds", "question": "Where does the sign of a refund get lost, and what does a month of net refunds show?",
                 "checks": ["o_refund_reduces_its_month", "o_refund_can_make_month_negative"],
                 "sources": ["ledger/parse.py"], "shortcut": "patch the report to subtract memo=='refund' rows"},
                {"id": "d_both", "question": "Do both fixes hold at once on data that exercises both?",
                 "checks": ["o_refunds_across_years_stay_separate"],
                 "sources": ["ledger/parse.py", "ledger/report.py"], "shortcut": "test each fix in isolation only"},
            ] + ([{"id": "d2_counts", "question": "What is a transaction for counting purposes, and how do count keys "
                   "relate to the totals' keys and order?",
                   "checks": ["o2_refund_counted", "o2_counts_order_matches_totals"],
                   "sources": ["ledger/report.py", "ledger/parse.py"],
                   "shortcut": "count only positive amounts, or return the dict in insertion order"}] if two_steps else []),
            "difficulty_case": {
                "first_try_mutant": "m_symptom_only",
                "hardest_decision": "Seeing that the refund sign is lost in the parser's abs(), one module away from "
                                    "the report the user complains about, rather than patching the report to subtract.",
                "first_try_verdict": "A strong model fixes the grouping key it can see in report.py, runs sample.csv, sees "
                                     "correct output, and ships; the refund defect never surfaces on the visible data.",
                "shortcut_test": "Two one-line edits in two modules score full marks, but only if both are found; the "
                                 "visible sample confirms the first and hides the second, so the shortest patch is not obvious.",
                "misreadings": [
                    {"trap": "t_symptom_patch", "misreading": "Grouping is fixed but ordering is still done on the month "
                     "token, so months from different years interleave.", "why_plausible": "The original sort was on the "
                     "month-only key and the one-year sample orders correctly either way.",
                     "diverges_on": "files spanning more than one year", "mutant": "m_month_sort"},
                ] + ([
                    {"trap": "t_step2_refund_counted", "misreading": "A refund reverses a sale, so it is not a transaction "
                     "to count; only positive amounts are counted.", "why_plausible": "Step 1 just made refunds "
                     "negative, and the totals logic treats them as adjustments rather than events.",
                     "diverges_on": "any month with a refund", "mutant": "m_step2_skip_refunds"},
                    {"trap": "t_step2_order", "misreading": "Counts come out in the order the rows were read, which is "
                     "already chronological for every file in the repository.", "why_plausible": "Every visible "
                     "input is in date order, so insertion order looks like the same order as the totals.",
                     "diverges_on": "a shuffled input file", "mutant": "m_step2_unordered"},
                ] if two_steps else []) + [
                    {"trap": "t_refund_sign", "misreading": "The report is where totals go wrong, so the report is where "
                     "the refund fix belongs; the parser is left with abs().", "why_plausible": "The user reported the "
                     "report; the parser looks like plain input validation.", "diverges_on": "any negative amount",
                     "mutant": "m_symptom_only"},
                ],
                "pitfalls_considered": [
                    {"pitfall": "Grouping by month token merges years; the visible sample hides it", "family": "symptom_vs_cause",
                     "where": "ledger/report.py", "decision": "planted", "trap": "t_symptom_patch"},
                    {"pitfall": "abs() in the parser silently flips refunds one module away from the symptom", "family": "decoy_location",
                     "where": "ledger/parse.py", "decision": "planted", "trap": "t_refund_sign"},
                    {"pitfall": "The CLI usage string has an obvious nit an eager fix would touch", "family": "scope",
                     "where": "ledger/cli.py", "decision": "planted", "trap": "t_scope"},
                    {"pitfall": "A malformed date with unpadded month could be accepted and mis-keyed", "family": "boundary",
                     "where": "ledger/parse.py", "decision": "rejected", "reason": "already rejected by the length check; not a decision"},
                    {"pitfall": "Decimal vs float rounding of totals", "family": "unit_or_format", "where": "ledger/report.py",
                     "decision": "rejected", "reason": "Decimal is used throughout; no rounding decision exists"},
                    {"pitfall": "README claims February is wrong only across years, a stale description", "family": "stale_docs",
                     "where": "README.md", "decision": "rejected", "reason": "the README agrees with the instruction"},
                    {"pitfall": "Refund memo text could be used as the signal instead of the sign", "family": "spec_reading",
                     "where": "ledger/parse.py", "decision": "rejected", "reason": "covered by the refund mutant's misreading"},
                    {"pitfall": "Adding a helper module violates the no-new-files rule", "family": "constraint",
                     "where": "ledger/", "decision": "planted", "trap": "t_scope"},
                ] + ([
                    {"pitfall": "Counting reuses the totals' sign handling and drops refunds as non-transactions",
                     "family": "spec_reading", "where": "ledger/report.py", "decision": "planted", "trap": "t_step2_refund_counted"},
                    {"pitfall": "Counts are returned in insertion order, which every in-order input file hides",
                     "family": "regression", "where": "ledger/report.py", "decision": "planted", "trap": "t_step2_order"},
                ] if two_steps else []),
            },
        },
    }


def write_bundle(root: Path, two_steps: bool = False) -> Path:
    from pipeline.author import build
    bundle = Path(root)
    repo = bundle / "environment/repo"
    for rel, text in repo_files().items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    build.build(spec(two_steps), bundle)
    if two_steps:
        # The user-turn index extraction writes for a real seed (msg 3 and msg 7
        # are the two turns the synthetic's steps cite; pivot 2).
        private = bundle / "authoring/extraction"
        private.mkdir(parents=True, exist_ok=True)
        (private / "user-turns.json").write_text(json.dumps(
            {"schema": "arena-coding/user-turns/1", "session_id": "synthetic", "pivot": 2, "span": [1, 9],
             "turns": [{"msg": i, "sha256": "0" * 64, "chars": 80, "undone": False} for i in (1, 3, 5, 7, 9)]},
            indent=2))
    from pipeline import naming
    from pipeline.ledger import faithfulness
    ledger = faithfulness.with_level(bundle, faithfulness.authored_ledger(bundle))
    (bundle / "provenance").mkdir(exist_ok=True)
    (bundle / "provenance/ledger.json").write_text(json.dumps(ledger, indent=2))
    naming.apply(bundle, ledger["faithfulness_level"])
    return bundle
