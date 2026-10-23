"""task.toml conversion: Arena's Q12 needs a deliverable's captured descendants collected."""
import tomllib

from pipeline import arena_v01 as a

OLD = {"task": {"name": "x", "version": "1.0.0", "description": "d"},
       "metadata": {"faithfulness_level": "F2", "source_trace": "s1", "language": "python"},
       "steps": [{"name": "01-a"}]}


def converted(deliverables=None, declared=None):
    meta = {**OLD["metadata"], **({"deliverables": deliverables} if deliverables else {})}
    return tomllib.loads(a.task_toml({**OLD, "metadata": meta}, env_id="a_f2_x", version="1.1.0", domain="d",
                                     prompt_language="en", declared=declared))


def artifacts(deliverables):
    return converted(deliverables)["artifacts"]


def test_file_deliverables_keep_cache_excludes():
    assert artifacts(["/app/repo/src/report.py"]) == [{"source": "/app/repo", "exclude": a.ARTIFACT_EXCLUDES}]


def test_a_whole_repo_deliverable_collects_everything():
    assert artifacts(["/app/repo"]) == [{"source": "/app/repo"}]


def test_the_graders_declared_deliverables_are_used_when_the_metadata_names_none():
    doc = converted(declared=["ai-chat/server.js", "ai-chat/server.js", "a/b.py"])
    assert doc["metadata"]["deliverables"] == ["/app/repo/a/b.py", "/app/repo/ai-chat/server.js"]
    assert doc["artifacts"] == [{"source": "/app/repo", "exclude": a.ARTIFACT_EXCLUDES}]
    assert converted()["metadata"]["deliverables"] == ["/app/repo"]
