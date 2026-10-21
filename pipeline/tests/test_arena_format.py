"""Arena's package format (Scaler x Arena weekly, 2026-10-22, "Formatting Changes - P0"): pipeline/arena_format.py
exports a delivered package without changing what the agent sees or the grader runs."""
import json
import shutil
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

from pipeline import arena_format, arena_repro, policy

STEP = "01-fix"
TASK_TOML = """schema_version = "1.4"
multi_step_reward_strategy = "mean"

[[artifacts]]
source = "/app/repo"

[task]
name = "arena/a_f2_example"
version = "1.1.0"
description = "An example task."

[metadata]
faithfulness_level = "F2"
language = "python"
source_trace = "trace-1"

[environment]
network_mode = "no-network"
cpus = 2
memory_mb = 4096

[agent]
network_mode = "no-network"
user = "agent"

[verifier]
environment_mode = "separate"

[[steps]]
name = "01-fix"
"""


def _write(path: Path, text, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    if mode:
        path.chmod(mode)


def delivered(root: Path) -> Path:
    """A minimal delivered package: one step, hidden data, one calibration run that edited app.py."""
    home = str(Path.home())
    pkg = root / "F2-scenario" / "a_f2_example"
    _write(pkg / "task.toml", TASK_TOML)
    _write(pkg / ".dockerignore", "tests/\nsolution/\n")
    _write(pkg / "environment/Dockerfile", "FROM python:3.12-slim\nRUN useradd --create-home --uid 1000 agent\n"
                                           "COPY repo/ /app/repo/\nUSER agent\n")
    _write(pkg / "environment/repo/app.py", "def add(a, b):\n    return a - b\n")
    _write(pkg / "environment/repo/data/prices.json", "{}\n")
    _write(pkg / f"steps/{STEP}/instruction.md", "add should add.\n")
    _write(pkg / f"steps/{STEP}/solution/solve.sh", "#!/bin/sh\ncd /app/repo && patch -p1 < /solution/fix.patch\n", 0o644)
    _write(pkg / f"steps/{STEP}/solution/fix.patch", "--- a/app.py\n+++ b/app.py\n")
    _write(pkg / "solution/solve.sh", "#!/bin/sh\n", 0o755)
    _write(pkg / "solution/README.md", "The reference fixes add.\n")
    _write(pkg / "tests/Dockerfile", "FROM python:3.12-slim\nCOPY . /tests/\nRUN chmod -R a-w /tests\n")
    _write(pkg / "tests/test.sh", "#!/bin/sh\npython3 /tests/grade.py\n", 0o644)
    _write(pkg / "tests/grade.py", "# shared grader\n")
    _write(pkg / "tests/suites/test_add.py", "import json\nDATA = 'data/cases.json'\n")
    _write(pkg / "tests/suites/data/cases.json", '[[1, 2, 3]]\n')
    _write(pkg / "tests/reward.toml", f'[[reward]]\nstep = "{STEP}"\nbands = {{ core = 100 }}\n')
    _write(pkg / "tests/checks.json", json.dumps({
        "schema": "arena-coding/checks/1", "toolchain": {"name": "python", "vendored": []},
        "deliverables": [{"kind": "file", "path": "app.py", "step": STEP}],
        "checks": [
            {"id": "o_add", "kind": "objective", "step": STEP, "band": "core", "weight": 100, "requires": [],
             "basis": "add adds.", "runner": {"type": "pytest", "select": ["test_add.py::test_add"]}},
            {"id": "g_prices", "kind": "guardrail", "step": STEP, "band": "core", "weight": 0, "requires": [],
             "basis": "prices stay.", "runner": {"type": "hash-guard", "paths": ["data/prices.json"]}}]}))
    _write(pkg / "provenance/ledger.json", json.dumps({
        "source": {"session_id": "trace-1", "seed_index": 1, "trace_sha256": "ab"}, "recovery_omissions": [],
        "entries": [{"path": "app.py", "origin": "lifted", "sha256": "x", "bytes": 30,
                     "citation": "trace-1::msg7::src/app.py"}], "faithfulness_level": "F2"}))
    _write(pkg / "authoring/spec.json", json.dumps({"steps": [{"name": STEP, "trace_turn": 3}],
                                                     "usable_corrections": 2, "source": {"tier": 1}}))
    _write(pkg / "authoring/traps.json", "[]\n")
    _write(pkg / "verification/controls.json", json.dumps({
        "ok": True, "errors": [], "results": {"nop": {"reward": 0.0}, "golden": {"reward": 1.0},
                                              "determinism": {"runs": 3, "identical": True, "rewards": [1, 1, 1]}}}))
    _write(pkg / "verification/delivery.json", json.dumps({"source": f"{home}/work/pkg", "acceptance_problems": []}))
    _write(pkg / "FAILURES.md", "# failures\n")
    _write(pkg / "REWARD-HACKING.md", "# hacking\n")
    campaign = pkg / "runs/claude-fable-5-1/calibration-abc"
    _write(campaign / "report.json", json.dumps({"agent": "claude-code", "model": "anthropic/claude-fable-5-1"}))
    _write(campaign / "runtime.json", json.dumps({"versions": {"harbor": "0.22.0"}, "binary": f"{home}/bin/harbor"}))
    _write(campaign / "package/task.toml", TASK_TOML.replace('version = "1.1.0"', 'version = "1.0.0"'))
    _write(campaign / "package/environment/repo/app.py", "def add(a, b):\n    return a - b\n")
    trial = campaign / "jobs/run/package__T1"
    _write(trial / "result.json", json.dumps({
        "agent_info": {"name": "claude-code", "version": "2.1"}, "started_at": "2026-10-18T10:00:00Z",
        "finished_at": "2026-10-18T10:05:00Z",
        "step_results": [{"step_name": STEP, "verifier_result": {"rewards": {"reward": 0.0}},
                          "agent_result": {"n_input_tokens": 10, "n_output_tokens": 5, "cost_usd": 0.1},
                          "agent_execution": {"started_at": "2026-10-18T10:00:00Z",
                                              "finished_at": "2026-10-18T10:03:00Z"}}]}))
    _write(trial / f"steps/{STEP}/artifacts/app/repo/app.py", "def add(a, b):\n    return a + b\n")
    _write(trial / f"steps/{STEP}/artifacts/app/repo/node_modules/x.js", "vendored\n")
    _write(trial / f"steps/{STEP}/agent/trajectory.json", json.dumps({"cwd": f"{home}/x"}))
    _write(pkg / "runs/index.json", json.dumps({"campaigns": [{
        "label": "claude-fable-5-1", "path": "runs/claude-fable-5-1/calibration-abc",
        "trials": [{"trial_id": "package__T1", "reward": 0.0, "exception": False,
                    "per_step_rubric": {STEP: {"score": 0.0, "criteria": {"o_add": 0.0, "g_prices": 1.0}}}}]}]}))
    return pkg


class ArenaFormatExport(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="arena-format-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = delivered(self.tmp / "src")
        self.pkg = arena_format.export(self.src, self.tmp / "out")

    def test_layout_passes_the_validator(self):
        self.assertEqual(arena_format.problems(self.pkg, require_receipt=False), [])
        self.assertEqual(self.pkg.name, "a_f2_example")
        self.assertFalse((self.pkg / "solution").exists())
        self.assertFalse((self.pkg / "runs").exists())
        self.assertTrue((self.pkg / "calibration/FAILURES.md").is_file())
        self.assertTrue((self.pkg / "verification/REWARD-HACKING.md").is_file())
        self.assertTrue(arena_format._executable(self.pkg / f"steps/{STEP}/solution/solve.sh"))
        self.assertTrue(arena_format._executable(self.pkg / "tests/test.sh"))

    def test_task_toml_gains_type_deliverables_protected_paths(self):
        cfg = tomllib.loads((self.pkg / "task.toml").read_text())
        self.assertEqual(cfg["type"], "coding")
        self.assertEqual(cfg["deliverables"], ["/app/repo/app.py"])
        self.assertIn("/app/repo/data/prices.json", cfg["protected_paths"])
        self.assertEqual(cfg["task"]["version"], "1.1.0")

    def test_hidden_data_moves_and_the_verifier_restores_it(self):
        self.assertFalse((self.pkg / "tests/suites/data").exists())
        moved = self.pkg / f"tests/expected/{STEP}/data/cases.json"
        self.assertEqual(moved.read_text(), '[[1, 2, 3]]\n')
        expected = json.loads((self.pkg / f"tests/expected/{STEP}/expected.json").read_text())
        self.assertEqual(expected["files"][0]["restored_to"], "/tests/suites/data/cases.json")
        dockerfile = (self.pkg / "tests/Dockerfile").read_text().splitlines()
        self.assertEqual(dockerfile[dockerfile.index("COPY . /tests/") + 2], arena_format.RESTORE_LINE)
        self.assertLess(dockerfile.index(arena_format.RESTORE_LINE), dockerfile.index("RUN chmod -R a-w /tests"))

    def test_rubric_agrees_with_checks(self):
        rubric = json.loads((self.pkg / f"tests/rubrics/{STEP}.json").read_text())
        self.assertEqual([(c["id"], c["weight"]) for c in rubric["criteria"]], [("o_add", 100)])
        self.assertEqual([g["id"] for g in rubric["gates"]], ["g_prices"])
        rubric["criteria"][0]["weight"] = 50
        (self.pkg / f"tests/rubrics/{STEP}.json").write_text(json.dumps(rubric))
        self.assertTrue(arena_format.rubric_problems(self.pkg, [STEP]))

    def test_per_run_diff_and_runs_csv(self):
        trial = self.pkg / "calibration/trials/claude-fable-5-1/package__T1"
        patch = (trial / f"steps/{STEP}/changes.patch").read_text()
        self.assertIn("-    return a - b", patch)
        self.assertIn("+    return a + b", patch)
        self.assertNotIn("node_modules", patch)
        run = json.loads((trial / "run.json").read_text())
        self.assertEqual(run["measured_version"], "1.0.0")
        self.assertEqual(run["steps"][0]["files_changed"], 1)
        rows = (self.pkg / "calibration/runs.csv").read_text().splitlines()
        self.assertEqual(rows[0].split(","), list(arena_format.RUNS_COLUMNS))
        self.assertEqual(len(rows), 2)
        metrics_doc = json.loads((self.pkg / "calibration/metrics.json").read_text())
        self.assertEqual(metrics_doc["models"]["claude-fable-5-1"]["pass_at_1"], 0.0)
        self.assertIn("version_note", metrics_doc)
        self.assertFalse((self.pkg / "calibration/campaigns/claude-fable-5-1/calibration-abc/package").exists())

    def test_operator_paths_are_scrubbed(self):
        self.assertNotIn(str(Path.home()), (self.pkg / "verification/delivery.json").read_text())
        self.assertEqual(arena_format.anonymity_problems(self.pkg), [])
        _write(self.pkg / "authoring/blob.bin", b"\xff\xfe" + str(Path.home()).encode())
        self.assertTrue(arena_format.anonymity_problems(self.pkg))

    def test_omissions_are_grouped_and_never_lifted_trees_skipped(self):
        omitted = ["node_modules", "kit/a.png", "kit/b.png", "kit/c.png", "/home/user/notes.txt"]
        got = arena_format._grouped(omitted, "absent", "provenance/source-scope.json")
        self.assertEqual([(d["path"], d.get("count")) for d in got], [("home/user/notes.txt", None), ("kit/", 3)])

    def test_provenance_and_declarations(self):
        scope = json.loads((self.pkg / "provenance/source-scope.json").read_text())
        self.assertEqual(scope["steps"][0]["user_message"], 3)
        self.assertEqual(scope["usable_corrections"], 2)
        self.assertIn(7, scope["messages_used"])
        ledger = json.loads((self.pkg / "provenance/ledger.json").read_text())
        self.assertEqual(ledger["files"][0]["source"], {"session_id": "trace-1", "message": 7, "path": "src/app.py"})
        self.assertEqual(json.loads((self.pkg / "declarations.json").read_text()), [])

    def test_zip_name_tree_digest_and_modes(self):
        z = arena_format.write_zip(self.pkg, self.tmp / "zips")
        self.assertEqual(z.name, "a_f2_example@1.1.0.zip")
        self.assertEqual(arena_format.zip_tree_sha256(z), arena_format.tree_sha256(self.pkg))
        with zipfile.ZipFile(z) as archive:
            self.assertEqual({n.split("/")[0] for n in archive.namelist()}, {"a_f2_example"})
            mode = archive.getinfo(f"a_f2_example/steps/{STEP}/solution/solve.sh").external_attr >> 16
            self.assertTrue(mode & 0o100)
        self.assertEqual(arena_format.zip_problems(z, require_receipt=False), [])
        slim = arena_format.slim_zip(z, self.tmp)
        self.assertEqual(arena_format.zip_tree_sha256(slim), arena_format.zip_tree_sha256(z))
        with zipfile.ZipFile(slim) as archive:
            self.assertFalse([n for n in archive.namelist() if "/calibration/" in n])

    def test_attached_receipt_keeps_the_tree_and_passes_both_gates(self):
        z = arena_format.write_zip(self.pkg, self.tmp / "zips")
        self.assertIn("verification/receipt.json missing", " ".join(arena_format.zip_problems(z)))
        doc = {"schema": arena_repro.SCHEMA, "zip": z.name, "zip_sha256": arena_repro.sha256(z),
               "tree_sha256": arena_format.zip_tree_sha256(z),
               "harness": {"name": "harbor", "version": policy.ARENA_HARBOR_VERSION,
                           "commit": policy.ARENA_HARBOR_COMMIT},
               "resources": dict(policy.ARENA_REPRO_RESOURCES),
               "oracle": {"reward": 1.0, "rewards": [1.0, 1.0], "exception": None},
               "nop": {"reward": 0.0, "exception": None}}
        receipt = arena_repro.receipt_path(z)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(doc))
        evidence = self.tmp / "evidence"
        for agent, reward in (("oracle", 1.0), ("nop", 0.0)):
            _write(evidence / agent / "t1" / f"steps/{STEP}/verifier/reward.json", json.dumps({"reward": reward}))
        _write(evidence / "oracle/t1" / f"steps/{STEP}/artifacts/app/repo/app.py", "def add(a, b):\n    return a + b\n")
        before = arena_format.zip_tree_sha256(z)
        arena_format.attach(z, doc, evidence)
        self.assertEqual(arena_format.zip_tree_sha256(z), before)
        self.assertEqual(arena_format.zip_problems(z), [])
        self.assertEqual(arena_repro.receipt_problems(z), [])     # bytes changed; the tree digest still binds
        with zipfile.ZipFile(z) as archive:
            got = json.loads(archive.read("a_f2_example/verification/receipt.json"))
            self.assertTrue(got["ok"])
            self.assertTrue(got["repeatable"]["identical"])
            self.assertIn("+    return a + b",
                          archive.read(f"a_f2_example/verification/oracle/after/run-1/{STEP}/changes.patch").decode())
        doc["tree_sha256"] = "0" * 64
        receipt.write_text(json.dumps(doc))
        self.assertIn("other bytes", " ".join(arena_repro.receipt_problems(z)))

    def test_failed_oracle_is_not_ok(self):
        z = arena_format.write_zip(self.pkg, self.tmp / "zips")
        doc = {"tree_sha256": arena_format.zip_tree_sha256(z), "harness": {"version": "0.20.0"},
               "oracle": {"reward": 0.5, "rewards": [0.5, 1.0]}, "nop": {"reward": 0.0}}
        arena_format.attach(z, doc, None)
        self.assertIn("oracle 0.5", " ".join(arena_format.zip_problems(z)))


class TaskTomlKeys(unittest.TestCase):
    def test_deprecated_keys_are_renamed(self):
        text = ('version = "1.0"\n\n[environment]\nallow_internet = false\nmemory = "4G"\nstorage = "10G"\n\n'
                '[task]\nversion = "2.0.0"\n')
        cfg = tomllib.loads(arena_format.rename_deprecated(text))
        self.assertEqual(cfg["schema_version"], "1.0")
        self.assertEqual(cfg["environment"], {"network_mode": "no-network", "memory_mb": 4096, "storage_mb": 10240})
        self.assertEqual(cfg["task"]["version"], "2.0.0")


if __name__ == "__main__":
    unittest.main()
