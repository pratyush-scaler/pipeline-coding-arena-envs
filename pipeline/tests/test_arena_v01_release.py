"""Release qualification on the Arena v0.1 package (user, 2026-10-24): the derived package is admissible evidence
for a version only when it reproduces from that version; everything else is refused."""
import importlib.util
import json
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from pipeline import arena_v01_release as v01
from pipeline.artifacts import atomic_json, identity
from pipeline.author.build import build
from pipeline.benchmark.support import copy_tree
from pipeline.tests.synthetic_code import write_bundle

ROOT = Path(__file__).resolve().parents[2]


def _version(root: Path) -> Path:
    bundle = root / "c-001-ledger-totals"
    bundle.mkdir()
    write_bundle(bundle)
    build(json.loads((bundle / "authoring/spec.json").read_text()), bundle)
    return bundle


def _campaign(root: Path, name: str, validation: dict) -> Path:
    out = root / name
    out.mkdir()
    (out / "report.json").write_text(json.dumps({"task_validation": validation}))
    return out


class Derivation(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.bundle = _version(self.root)
        self.params = v01.write_target(self.bundle, domain="finance", prompt_language="en")
        v01._REPRODUCED.clear()
        self.made = v01.derive(self.bundle, self.root / "derived", self.params)
        self.pkg = Path(self.made["package"])

    def run_copy(self, name="run") -> Path:
        """What Harbor's runner copies into a campaign: the task surfaces only (no sidecar)."""
        out = self.root / name / "package"
        copy_tree(self.pkg, out, task_only=True)
        return out

    def test_target_defaults(self):
        self.assertEqual(self.params["lineage"], "c-001-ledger-totals")
        self.assertEqual(self.params["version"], "1.1.0")
        self.assertEqual(v01.target(self.bundle), self.params)

    def test_the_package_is_the_v01_runtime_with_the_bundles_agent_surface(self):
        self.assertEqual(self.made["env_id"], f"a_{self.params['level'].lower()}_ledger-totals")
        meta = tomllib.loads((self.pkg / "task.toml").read_text())
        self.assertEqual(meta["metadata"]["delivery_format"], "0.1")
        self.assertEqual(meta["task"]["version"], "1.1.0")
        self.assertTrue((self.pkg / "tests/arena_details.py").is_file())
        for rel in ("environment", "steps"):
            for f in (self.bundle / rel).rglob("*"):
                if f.is_file() and f.name != "solve.sh":
                    self.assertEqual(f.read_bytes(), (self.pkg / f.relative_to(self.bundle)).read_bytes(), f)
        self.assertEqual((self.pkg / "tests/grade.py").read_bytes(), (self.bundle / "tests/grade.py").read_bytes())

    def test_preflight_admits_the_derived_package_as_this_version(self):
        from pipeline.benchmark.preflight import validate_task
        validation = validate_task(self.pkg)
        self.assertEqual(validation["mode"], v01.MODE)
        self.assertTrue(validation["current_generator_verified"])
        self.assertEqual(validation["artifact_sha256"], identity(self.bundle))
        self.assertEqual(validation["derivation"]["task_tree_sha256"], self.made["task_tree_sha256"])

    def test_preflight_refuses_a_package_that_no_longer_reproduces(self):
        from pipeline.benchmark.preflight import validate_task
        grade = self.pkg / "tests/grade.py"
        grade.write_text(grade.read_text() + "\n# edited after derivation\n")
        v01._REPRODUCED.clear()
        with self.assertRaisesRegex(ValueError, "derivation"):
            validate_task(self.pkg)

    def test_preflight_refuses_a_drifted_source_copy(self):
        from pipeline.benchmark.preflight import validate_task
        instruction = next((self.pkg / v01.SIDECAR / "source/steps").rglob("instruction.md"))
        instruction.write_text("something else\n")
        with self.assertRaisesRegex(ValueError, "derivation"):
            validate_task(self.pkg)

    def test_a_converted_package_without_a_derivation_is_not_current(self):
        from pipeline.benchmark.preflight import validate_task
        shutil.rmtree(self.pkg / v01.SIDECAR)
        with self.assertRaisesRegex(ValueError, "legacy"):
            validate_task(self.pkg)

    def test_snapshot_is_this_versions_evidence_only_when_it_reproduces(self):
        validation = v01.validation(self.pkg)
        package = self.run_copy()
        self.assertEqual(v01.snapshot_problems(self.bundle, package, validation), [])
        # another target's package
        other = dict(validation, derivation=dict(validation["derivation"],
                                                 params=dict(self.params, level="F3")))
        self.assertTrue(v01.snapshot_problems(self.bundle, package, other))
        # a tree edited after the run copy
        (package / "tests/grade.py").write_text("print('ok')\n")
        self.assertTrue(v01.snapshot_problems(self.bundle, package, validation))

    def test_another_version_cannot_claim_the_campaign(self):
        validation = v01.validation(self.pkg)
        package = self.run_copy()
        instruction = next((self.bundle / "steps").rglob("instruction.md"))
        instruction.write_text(instruction.read_text() + "\nOne more sentence.\n")
        self.assertEqual(v01.snapshot_problems(self.bundle, package, validation),
                         [f"native snapshot is not the {v01.FORMAT} derivation of this task"])

    def test_native_evidence_reads_a_derived_campaign_through_the_derivation(self):
        from pipeline.evidence import native_evidence
        validation = v01.validation(self.pkg)
        out = self.root / "campaign"
        copy_tree(self.pkg, out / "package", task_only=True)
        (out / "report.json").write_text(json.dumps({"task_validation": validation, "requested_model": None}))
        (out / "harbor-job.json").write_text("{}")
        errors, _ = native_evidence(self.bundle, out, "oracle")
        self.assertFalse([e for e in errors if "current generator" in e or "snapshot" in e], errors)
        (out / "package/tests/grade.py").write_text("print('ok')\n")
        errors, _ = native_evidence(self.bundle, out, "oracle")
        self.assertIn(f"native snapshot is not the {v01.FORMAT} derivation of this task", errors)

    def test_release_finish_packaging_reproduces_the_calibrated_tree(self):
        atomic_json(self.bundle / "verification/coverage-critic.json", {"gaps": []})  # a delivered receipt
        atomic_json(self.bundle / v01.RELEASE_RECORD, {"ready": True, "calibration": {
            "format": v01.FORMAT, "target": self.params,
            "package": {k: self.made[k] for k in ("env_id", "version", "task_tree_sha256")}}})
        with mock.patch("pipeline.ledger.levels.assign_measured", return_value={"level": self.params["level"]}), \
                mock.patch("pipeline.ledger.levels.calibration_reproduces_failure", return_value=None):
            made = v01.package(self.bundle, self.root / "packages")
        self.assertEqual(made["task_tree_sha256"], self.made["task_tree_sha256"])
        self.assertTrue((Path(made["source_package"]) / "verification/coverage-critic.json").is_file())
        # a release that calibrated another tree is refused
        atomic_json(self.bundle / v01.RELEASE_RECORD, {"ready": True, "calibration": {
            "format": v01.FORMAT, "target": self.params,
            "package": {"env_id": self.made["env_id"], "version": "1.1.0", "task_tree_sha256": "0" * 64}}})
        with mock.patch("pipeline.ledger.levels.assign_measured", return_value={"level": self.params["level"]}), \
                self.assertRaisesRegex(ValueError, "not what this version derives"):
            v01.package(self.bundle, self.root / "packages")


class ReleaseSide(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.params = {"lineage": "c-001-x", "level": "F2", "version": "1.1.0", "domain": "d", "prompt_language": "en"}

    def test_campaigns_must_measure_what_the_version_releases_on(self):
        native = _campaign(self.root, "native", {"mode": "current-draft"})
        derived = _campaign(self.root, "derived", {"mode": v01.MODE, "derivation": {"params": self.params}})
        other = _campaign(self.root, "other", {"mode": v01.MODE,
                                                "derivation": {"params": dict(self.params, level="F1")}})
        self.assertEqual(v01.target_problems(native, None), [])
        self.assertTrue(v01.target_problems(derived, None))
        self.assertTrue(v01.target_problems(native, self.params))
        self.assertEqual(v01.target_problems(derived, self.params), [])
        self.assertTrue(v01.target_problems(other, self.params))
        self.assertTrue(v01.target_problems(self.root / "missing", self.params))
        self.assertEqual(v01.target_problems(self.root / "missing", None), [])

    def test_a_retained_calibration_on_the_bundle_is_not_reused_for_a_v01_target(self):
        from pipeline import release
        bundle = self.root / "v"
        (bundle / "verification").mkdir(parents=True)
        native = _campaign(self.root, "native", {"mode": "current-draft"})
        atomic_json(bundle / "verification/native-calibrations.json",
                    {"artifact_sha256": "sha", "campaigns": [str(native)]})
        measured = ([], {"model": "anthropic/claude-fable-5-1"})
        with mock.patch("pipeline.release.identity", return_value="sha"), \
                mock.patch("pipeline.release.calibration_measurement", return_value=measured):
            self.assertTrue(release._calibrated(bundle, ["claude-fable-5-1"], 10))
            atomic_json(bundle / v01.TARGET, {"schema": v01.SCHEMA + "/target", "params": self.params})
            self.assertFalse(release._calibrated(bundle, ["claude-fable-5-1"], 10))

    def test_qualify_runs_controls_and_calibration_on_the_derived_package(self):
        from contextlib import ExitStack
        from pipeline import release
        bundle = self.root / "v"
        (bundle / "authoring").mkdir(parents=True)
        (bundle / "authoring/spec.json").write_text(json.dumps({"_producer": {"model": "author"}}))
        atomic_json(bundle / "verification/semantic-review.json", {"artifact_sha256": "sha", "model": "critic"})
        ran, calibrated = [], []

        def campaign(snapshot, output, agent, **_):
            ran.append((agent, Path(snapshot)))
            return {"ok": True}

        class Capacity:
            def reserve(self, _n):
                from contextlib import nullcontext
                return nullcontext()

        def evidence(_bundle, path, role, minimum=1):
            return [], {"trials": [{"reward": 1 if role == "oracle" else 0}]}

        def calibrate(_bundle, snapshot, _root, model, *_a, **_k):
            calibrated.append((model, Path(snapshot)))
            return f"/c/{model}", [], True

        with ExitStack() as stack:
            for name, value in {"pipeline.release._calibrated": True, "pipeline.local_work.enabled": False,
                                "pipeline.release._preflight": True, "pipeline.artifacts.freeze": "sha",
                                "pipeline.arena_v01_release.target": self.params,
                                "pipeline.arena_v01_release.derive": {"package": "/derived/a_f2_x"},
                                "pipeline.arena_v01_release.target_problems": [],
                                "pipeline.benchmark.coder.native_runner": (campaign, Capacity()),
                                "pipeline.review.review_problems": [], "pipeline.review.semantic_rejection": False,
                                "pipeline.release._pass_audit": {},
                                "pipeline.release._evaluate": {"release_ready": False}}.items():
                stack.enter_context(mock.patch(name, return_value=value))
            stack.enter_context(mock.patch("pipeline.evidence.native_evidence", side_effect=evidence))
            stack.enter_context(mock.patch("pipeline.release._calibrate_model", side_effect=calibrate))
            release._qualify(bundle, ["one", "two"], "critic", "two", self.root / "native", 10, 2)
        self.assertEqual(sorted(ran), [("nop", Path("/derived/a_f2_x")), ("oracle", Path("/derived/a_f2_x"))])
        self.assertEqual(sorted(calibrated), [("one", Path("/derived/a_f2_x")), ("two", Path("/derived/a_f2_x"))])

    def test_a_level_claim_the_calibration_does_not_support_is_refused(self):
        bundle = self.root / "v"
        (bundle / "provenance").mkdir(parents=True)
        (bundle / "provenance/ledger.json").write_text("{}")
        with mock.patch("pipeline.ledger.levels.assign_measured", return_value={"level": "F0"}):
            self.assertTrue(v01.level_problems(bundle, dict(self.params, level="F1")))
        with mock.patch("pipeline.ledger.levels.assign_measured", return_value={"level": "F1"}):
            self.assertEqual(v01.level_problems(bundle, dict(self.params, level="F1")), [])

    def test_the_release_record_names_its_calibration_and_holds_the_level(self):
        from pipeline import shipfix
        version = self.root / "c-001-x"
        (version / "verification").mkdir(parents=True)
        (version / "task.toml").write_text('[task]\nname = "arena/x"\nversion = "1.0.0"\n')
        (version / "verification/qualification.json").write_text(json.dumps({"evidence": {"task_metrics": {
            "anthropic/claude-fable-5-1": {"pass_at_1": 0.1, "episodes_valid": 10},
            "openai/gpt-6-astra": {"pass_at_1": 0.2, "episodes_valid": 10}}}}))
        calibration = {"format": v01.FORMAT, "harbor": ["0.24.0"], "target": None, "campaigns": {}}

        def run(level_problems):
            patches = [
                mock.patch("pipeline.author.controls.problems", return_value=[]),
                mock.patch("pipeline.author.probes.problems", return_value=[]),
                mock.patch("pipeline.benchmark.tasks_check.retained", return_value={"status": "pass"}),
                mock.patch("pipeline.coverage_critic.ship_critic", return_value={"gaps": [], "mode": "verify"}),
                mock.patch("pipeline.shipcheck.coverage", return_value=[]),
                mock.patch("pipeline.shipfix.review_model", return_value="critic"),
                mock.patch("pipeline.release.qualify", return_value={"status": "accepted", "release_ready": True}),
                mock.patch("pipeline.shipfix.calibration_record", return_value={}),
                mock.patch("pipeline.genuine.audit", return_value={"certified": True, "episodes": []}),
                mock.patch("pipeline.learning_signal.from_release", return_value={}),
                mock.patch("pipeline.learning_signal.problems", return_value=[]),
                mock.patch("pipeline.learning_signal.advisories", return_value=[]),
                mock.patch("pipeline.shipcheck.sweep", return_value={"ok": True, "open": 0, "findings": []}),
                mock.patch("pipeline.arena_v01_release.calibration_of_record", return_value=calibration),
                mock.patch("pipeline.arena_v01_release.level_problems", return_value=level_problems)]
            for p in patches:
                p.start()
            try:
                return shipfix.release(version, {"domain": "d", "prompt_language": "en", "level": "F2"})
            finally:
                for p in patches:
                    p.stop()

        record = run([])
        self.assertTrue(record["ready"], json.dumps(record, default=str)[:2000])
        self.assertEqual(record["calibration"]["format"], v01.FORMAT)
        self.assertEqual(record["arena_v01_target"]["level"], "F2")
        self.assertEqual(v01.target(version)["lineage"], "c-001-x")
        record = run(["claims F2 but measures F0"])
        self.assertFalse(record["ready"])
        self.assertEqual(record["steps"]["arena_v01"]["result"]["not_ready"], ["claims F2 but measures F0"])


class Cli(unittest.TestCase):
    def test_arena_shipfix_forwards_the_v01_target(self):
        from pipeline import cli
        target = '{"domain": "d", "prompt_language": "en"}'
        with mock.patch.object(cli, "load_keys", return_value={}), \
                mock.patch.object(cli, "_run", return_value=0) as run:
            cli.main(["shipfix", "/v", "--release", "--arena-v01", target])
        run.assert_called_once_with("pipeline.shipfix", "/v", "--release", "--arena-v01", target)


def _ship():
    spec = importlib.util.spec_from_file_location("arena_v01_ship_release", ROOT / "orchestration/arena_v01_ship.py")
    ship = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ship)
    return ship


class Orchestration(unittest.TestCase):
    def test_run_launches_the_pipeline_command_from_the_staged_code(self):
        ship = _ship()
        with mock.patch.object(ship, "stage", return_value=(Path("/s"), "sha")), \
                mock.patch.object(ship, "_start", return_value="rid") as start:
            self.assertEqual(ship.run("ws", ["shipfix", "/b", "--release"]), "rid")
        start.assert_called_once_with("ws", Path("/s"), "sha", ["shipfix", "/b", "--release"])

    def test_a_delivered_packages_layout_and_history_are_unchanged(self):
        ship = _ship()
        row = {"from_version": "1.0.0", "delivered_on": "2026-10-01", "version": "1.1.0"}
        lay = ship._layout("a_f2_x", row)
        self.assertEqual(lay["oracle"], ship.CAMPAIGNS / "a_f2_x/cal-oracle")
        self.assertEqual(lay["models"]["gpt-6-astra"], ship.CAMPAIGNS / "a_f2_x/cal-gpt-6-astra")
        self.assertIsNone(lay["bundle"])
        self.assertIn("Harbor 0.22.0", ship._history(row)[0]["summary"])

    def test_a_released_package_reads_the_releases_campaigns(self):
        ship = _ship()
        cal = {"oracle": "/n/oracle", "nop": "/n/nop", "campaigns": {
            "anthropic/claude-fable-5-1": ["/n/f"], "openai/gpt-6-astra": ["/n/a"]}}
        row = {"version": "1.1.0", "from_version": None, "release": {"bundle": "/b", "calibration": cal}}
        lay = ship._layout("a_f2_x", row)
        self.assertEqual((lay["oracle"], lay["models"]["claude-fable-5-1"], lay["bundle"]),
                         (Path("/n/oracle"), Path("/n/f"), Path("/b")))
        history = ship._history(row)
        self.assertEqual([h["version"] for h in history], ["1.1.0"])
        cal["campaigns"]["openai/gpt-6-astra"].append("/n/a-topup")
        with self.assertRaises(SystemExit):
            ship._layout("a_f2_x", row)


if __name__ == "__main__":
    unittest.main()
