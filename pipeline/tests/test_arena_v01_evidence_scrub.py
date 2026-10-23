"""Agent-log copying for Arena's leak scan: lossless JSON escapes, and redaction of harness credentials."""
import json

import pytest

from pipeline import arena_v01_evidence as ev


def test_decorator_after_escaped_newline_or_diff_marker_is_escaped_losslessly():
    raw = json.dumps({"t": "x\n@pytest.fixture\n+@pytest.mark.parametrize('a', [1])\n-@reg.register"})
    out = ev.escape_json_text(raw)
    assert "n@pytest" not in out and "n+@pytest" not in out and "n-@reg" not in out
    assert json.loads(out) == json.loads(raw)


def test_url_userinfo_is_escaped_but_a_real_address_is_not():
    raw = json.dumps({"cmd": "curl 'http://localhost/?u=https://fr.wikipedia.org@evil.com/x' bob@example.org"})
    out = ev.escape_json_text(raw)
    assert "org@evil.com" not in out and "bob@example.org" in out
    assert json.loads(out) == json.loads(raw)


def test_credential_shape_is_escaped_only_inside_opaque_blobs():
    blob = "Qm9n" * 20 + "AIza" + "x" * 40 + "Zm9v" * 10
    raw = json.dumps({"signature": blob, "text": "key AIza" + "y" * 35})
    out = ev.escape_json_text(raw)
    assert "AIza" + "x" * 40 not in out and "AIza" + "y" * 35 in out
    assert json.loads(out) == json.loads(raw)


def test_harness_secret_is_redacted_and_never_allowed_in_captured_bytes(tmp_path, monkeypatch):
    secret = "sk-ant-usr-" + "A1b2_C3d4-" * 9
    monkeypatch.setattr(ev, "SECRETS", [secret])
    src = tmp_path / "t.json"
    src.write_text(json.dumps({"out": f"ANTHROPIC_API_KEY={secret}\nX=1"}))
    assert ev.copy_json_evidence(src, tmp_path / "o.json") == 1
    text = (tmp_path / "o.json").read_text()
    assert secret not in text and ev.REDACTED in text
    with pytest.raises(ValueError):
        ev.assert_no_secret(f"token {secret}".encode(), "capture")



def test_every_at_in_a_url_authority_is_escaped():
    raw = json.dumps({"cmd": 'run("https://user:pw@evil.com@api.groq.com/")'})
    out = ev.escape_json_text(raw)
    assert "pw@evil" not in out and "com@api" not in out and json.loads(out) == json.loads(raw)


def test_log_addresses_outside_task_content_are_redacted(tmp_path):
    (tmp_path / "environment").mkdir()
    (tmp_path / "environment/users.csv").write_text("ana@fixture-shop.com\n")
    log = tmp_path / "calibration/trials/m/r/steps/s/agent/trajectory.json"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"t": "ana@fixture-shop.com wrote; search: enterprise@cloudflare.com\nbob@corp-mail.com"}))
    assert ev.redact_log_addresses(tmp_path) == 2
    doc = json.loads(log.read_text())["t"]
    assert "ana@fixture-shop.com" in doc and "cloudflare" not in doc and "corp-mail" not in doc
    assert doc.count(ev.REDACTED_EMAIL) == 2 and "\n" in doc
