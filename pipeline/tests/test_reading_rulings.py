"""A human reading ruling stands in for a judge's refusal, never for a judgement."""
import json

from pipeline import attribution


def _write(tmp_path, ruling):
    (tmp_path / "verification").mkdir()
    (tmp_path / attribution.READING_RULINGS).write_text(json.dumps(ruling))


RULING = {"adjudicator": "human", "checks": {"s/o_x": {"verdict": "settled", "grading_sha256": "abc"}}}


def test_a_refusal_beside_a_settled_answer_can_be_ruled(tmp_path):
    _write(tmp_path, RULING)
    entry = {"judges": {"a": {"outcome": "settled"}, "b": {"outcome": "error", "error": "refused"}}}
    assert attribution.human_reading_ruling(tmp_path, "abc", "s/o_x", entry)


def test_an_unsettled_answer_is_never_overruled(tmp_path):
    _write(tmp_path, RULING)
    entry = {"judges": {"a": {"outcome": "unsettled"}, "b": {"outcome": "error"}}}
    assert attribution.human_reading_ruling(tmp_path, "abc", "s/o_x", entry) is None


def test_the_ruling_is_bound_to_the_grading(tmp_path):
    _write(tmp_path, RULING)
    entry = {"judges": {"a": {"outcome": "settled"}, "b": {"outcome": "error"}}}
    assert attribution.human_reading_ruling(tmp_path, "other", "s/o_x", entry) is None


def test_no_answer_at_all_is_not_ruled(tmp_path):
    _write(tmp_path, RULING)
    entry = {"judges": {"a": {"outcome": "error"}, "b": {"outcome": "error"}}}
    assert attribution.human_reading_ruling(tmp_path, "abc", "s/o_x", entry) is None
