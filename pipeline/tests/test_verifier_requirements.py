"""The verifier's requirements.txt leaves out only a pin that collides with the grader's test pins.

c-043 (2026-10-07): Arena's C-015 fix installed the repo's declared pytest==7.4.2 in the agent image;
the verifier installs requirements.txt and requirements-test.txt (pytest==8.3.4) in one pip call, so
its image stopped building. The agent keeps the declared version; the grader keeps its own.
"""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline.author import build, toolchain
from pipeline.gates import code as gates_code

TEST = list(toolchain.TEST_DEPS["python"])


class VerifierRequirements(unittest.TestCase):
    def test_a_conflicting_exact_pin_is_left_out_of_the_verifier_copy_only(self):
        text = "fastapi==0.103.1\npytest==7.4.2\nrequests==2.34.2\n"
        out = build.verifier_requirements(text, TEST)
        self.assertNotIn("\npytest==7.4.2", "\n" + out)
        self.assertIn("fastapi==0.103.1", out)
        self.assertIn("requests==2.34.2", out)
        self.assertTrue(out.startswith("# verifier: pytest==7.4.2 left out"))

    def test_anything_that_already_resolves_is_byte_identical(self):
        for text in ("", "fastapi==0.103.1\n", "pytest==8.3.4\nflask==3.0.0\n", "pytest-timeout==2.3.1\n"):
            self.assertIs(build.verifier_requirements(text, TEST), text)

    def test_names_normalise_and_markers_are_tolerated(self):
        out = build.verifier_requirements('Pytest_Timeout == 2.1.0 ; python_version>"3"\nflask==3.0.0\n', TEST)
        self.assertEqual(out.splitlines()[1:], ["flask==3.0.0"])

    def test_test_deps_and_the_drift_gate_agree(self):
        spec = {"deps": {"requirements.txt": "fastapi==0.103.1\npytest==7.4.2\n"}}
        deps = build._test_deps(spec, {"name": "python"})
        self.assertEqual(deps["requirements.txt"], build.verifier_requirements(spec["deps"]["requirements.txt"], TEST))
        self.assertEqual(spec["deps"]["requirements.txt"], "fastapi==0.103.1\npytest==7.4.2\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "environment/deps").mkdir(parents=True)
            (root / "tests/deps").mkdir(parents=True)
            (root / "environment/deps/requirements.txt").write_text(spec["deps"]["requirements.txt"])
            for name, text in deps.items():
                (root / "tests/deps" / name).write_text(text)

            class Ctx:
                task_dir = root

                def path(self, rel):
                    return root / rel

            drift = [f for f in self._deps_findings(Ctx()) if "deps-drift" in str(f)]
            self.assertEqual(drift, [])
            (root / "tests/deps/requirements.txt").write_text("fastapi==0.103.1\n")
            drift = [f for f in self._deps_findings(Ctx()) if "deps-drift" in str(f)]
            self.assertEqual(len(drift), 1)

    @staticmethod
    def _deps_findings(ctx):
        """Only the deps part of _toolchain_pinned (no Dockerfiles in this tree)."""
        orig = gates_code.toolchain.dockerfile_problems
        gates_code.toolchain.dockerfile_problems = lambda _d: []
        try:
            for rel in ("environment/Dockerfile", "tests/Dockerfile"):
                (ctx.task_dir / rel).write_text("FROM x@sha256:0\n")
            findings, _ = gates_code._toolchain_pinned(ctx)
        finally:
            gates_code.toolchain.dockerfile_problems = orig
        return [json.dumps(f.__dict__, default=str) if hasattr(f, "__dict__") else str(f) for f in findings]


if __name__ == "__main__":
    unittest.main()
