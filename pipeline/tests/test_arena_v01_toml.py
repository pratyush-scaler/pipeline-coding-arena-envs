"""task.toml conversion: Arena's Q12 needs a deliverable's captured descendants collected."""
import tomllib

from pipeline import arena_v01 as a

OLD = {"task": {"name": "x", "version": "1.0.0", "description": "d"},
       "metadata": {"faithfulness_level": "F2", "source_trace": "s1", "language": "python"},
       "steps": [{"name": "01-a"}]}


def artifacts(deliverables):
    old = {**OLD, "metadata": {**OLD["metadata"], "deliverables": deliverables}}
    return tomllib.loads(a.task_toml(old, env_id="a_f2_x", version="1.1.0", domain="d", prompt_language="en"))["artifacts"]


def test_file_deliverables_keep_cache_excludes():
    assert artifacts(["/app/repo/src/report.py"]) == [{"source": "/app/repo", "exclude": a.ARTIFACT_EXCLUDES}]


def test_a_whole_repo_deliverable_collects_everything():
    assert artifacts(["/app/repo"]) == [{"source": "/app/repo"}]
