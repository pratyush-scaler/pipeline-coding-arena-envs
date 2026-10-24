"""The Arena v0.1 ship gate: Arena's checker report decides, and only a supplier-approved waiver removes a FAIL."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("arena_v01_ship", ROOT / "orchestration/arena_v01_ship.py")
ship = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ship)

REPORT = [{"subject": "a_f2_x@1.1.0.zip", "failed": True, "rows": [
    {"id": "Q1", "status": "PASS"}, {"id": "Q14", "status": "WARN"}, {"id": "Q22", "status": "SKIP"},
    {"id": "Q21", "status": "FAIL"}, {"id": "Q25", "status": "FAIL"}]}]


def test_every_fail_blocks_and_nothing_else_does():
    assert ship.failures(REPORT) == ["Q21", "Q25"]


def test_a_waiver_removes_only_its_own_check():
    assert ship.failures(REPORT, {"Q25"}) == ["Q21"]
    assert ship.failures(REPORT, {"Q25", "Q21"}) == []


def test_waivers_are_human_notes_per_check():
    doc = json.loads(ship.WAIVERS.read_text())
    for env, table in doc.items():
        if env.startswith("_"):
            continue
        assert table and all(k.startswith("Q") and len(v) > 80 for k, v in table.items()), env


def test_reports_name_no_host_path():
    text = f"source: {ship.LIVE}/envs/c-012-react-web-app/verification/x.json"
    assert ship.scrub_report(text, "a_f2_react-web-app", "x") == \
        "source: <pipeline>/envs/a_f2_react-web-app/verification/x.json"
    with pytest.raises(SystemExit):
        ship.scrub_report("/home/pratyush/notes", "a_f2_x", "x")


def test_q37_counts_only_a_supplier_name_standing_as_a_word(tmp_path):
    import zipfile
    clean = tmp_path / "a@1.0.0.zip"
    with zipfile.ZipFile(clean, "w") as z:
        z.writestr("a/lexicon.js", "escalera StandardScaler MinMaxScaler upscaler")
    assert ship.identity_hits(clean) == []
    named = tmp_path / "b@1.0.0.zip"
    with zipfile.ZipFile(named, "w") as z:
        z.writestr("b/README.md", "Built by Scaler AI Labs")
        z.writestr("b/scaler/x.txt", "")
    assert ship.identity_hits(named) == ["b/README.md", "b/scaler/x.txt"]
