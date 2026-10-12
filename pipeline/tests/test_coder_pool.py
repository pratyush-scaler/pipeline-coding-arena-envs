"""The Coder calibration workspace: one only, shared slots, pool results, login."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.benchmark import coder
from pipeline.benchmark.coder import Pool, Settings


# The pause guard (`coder._remote_run_alive`) reads this checkout's real remote
# launch records; these tests exercise pausing itself, so no remote run exists.
_NO_REMOTE_RUN = patch.object(coder, "_remote_run_alive", return_value=False)


def setUpModule():
    _NO_REMOTE_RUN.start()


def tearDownModule():
    _NO_REMOTE_RUN.stop()


def cfg(**kw):
    base = dict(template="t", copy_parameters_from="ref",
                tasks_per_instance=20, idle_stop_after=0)
    base.update(kw)
    return Settings(**base)


class FastPool(Pool):
    """Provisioning is instant and never touches a deployment."""
    provisioned: list

    def __init__(self, settings, fail=()):
        super().__init__(settings)
        self.checked_login = True
        self.fail = set(fail)
        self.provisioned = []

    def _existing(self):
        return {}

    def _provision(self, inst):
        self.provisioned.append(inst.name)
        with self.cond:
            inst.state = "failed" if inst.name in self.fail else "ready"
            inst.error = "boom" if inst.name in self.fail else None
            self.cond.notify_all()


class SingleWorkspace(unittest.TestCase):
    """Operator rule: calibration uses exactly one Coder workspace."""

    def test_every_campaign_runs_on_the_one_workspace(self):
        p = FastPool(cfg())
        self.assertEqual({p.acquire().name for _ in range(20)}, {"arena-calib-1"})
        self.assertEqual(len(p.instances), 1)
        self.assertEqual(p.provisioned, ["arena-calib-1"])

    def test_a_full_workspace_makes_campaigns_wait_and_never_adds_another(self):
        p = FastPool(cfg(tasks_per_instance=20))
        held = [p.acquire() for _ in range(20)]
        got = []
        waiter = threading.Thread(target=lambda: got.append(p.acquire()))
        waiter.start()
        time.sleep(0.2)
        self.assertEqual(got, [])                      # the 21st campaign waits...
        self.assertEqual(len(p.instances), 1)          # ...and no second workspace appears
        p.release(held[0])
        waiter.join(timeout=5)
        self.assertEqual(got[0].name, "arena-calib-1")
        self.assertEqual(p.provisioned, ["arena-calib-1"])

    def test_a_failed_workspace_is_loud_and_never_replaced(self):
        p = FastPool(cfg(), fail={"arena-calib-1"})
        with self.assertRaises(RuntimeError) as caught:
            p.acquire()
        self.assertIn("boom", str(caught.exception))
        self.assertEqual(p.provisioned, ["arena-calib-1"])

    def test_concurrent_acquires_never_oversubscribe(self):
        p = FastPool(cfg(tasks_per_instance=4))
        out, peak, lock = [], [0], threading.Lock()

        def work():
            inst = p.acquire()
            with lock:
                out.append(inst.name)
                peak[0] = max(peak[0], inst.load)
            time.sleep(0.02)
            p.release(inst)
        threads = [threading.Thread(target=work) for _ in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(out), 30)
        self.assertEqual(len(p.instances), 1)
        self.assertLessEqual(peak[0], 4)
        self.assertEqual(p.instances[0].load, 0)

    def test_a_campaign_larger_than_the_workspace_is_refused(self):
        with self.assertRaises(ValueError):
            FastPool(cfg(tasks_per_instance=4)).acquire(5)


class CampaignRouting(unittest.TestCase):
    def test_results_are_pooled_back_and_trial_concurrency_is_capped(self):
        p = FastPool(cfg(trials_per_task=1))
        seen = []

        def fake_run(self, bundle, output, kwargs, slots=1):
            seen.append((self.name, kwargs["workers"]))
            return {"ok": True, "output": str(output)}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(coder, "pool", return_value=p), \
                patch.object(coder.Instance, "run", fake_run):
            report = coder.run_campaign(Path(tmp) / "bundle", Path(tmp) / "out", workers=10,
                                        model="m", agent="terminus-2")
        self.assertEqual(report["output"], str(Path(tmp) / "out"))
        self.assertEqual(seen, [("arena-calib-1", 1)])
        self.assertEqual(p.instances[0].load, 0)       # slot released after the run

    def test_calibration_uses_coder_unless_local_is_chosen_explicitly(self):
        with patch.dict(os.environ, {"ARENA_NATIVE_BACKEND": "coder"}):
            run, capacity = coder.native_runner("calibration")
            self.assertIs(run, coder.run_campaign)
            with capacity.reserve(7) as slots:
                self.assertEqual(slots, 7)
        with patch.dict(os.environ, {"ARENA_NATIVE_BACKEND": "local"}):
            with patch("pipeline.benchmark.harbor.run_campaign", return_value={"local": True}):
                run, _ = coder.native_runner("calibration")
                self.assertEqual(run("b", "o"), {"local": True})
        with patch.dict(os.environ, {"ARENA_NATIVE_BACKEND": "elsewhere"}):
            with self.assertRaises(ValueError):
                coder.native_runner("calibration")

    def test_a_test_process_never_reaches_a_real_deployment(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARENA_NATIVE_BACKEND", None)
            self.assertEqual(coder._default_backend(), "local")
            with self.assertRaises(RuntimeError):
                coder.pool()

    def test_production_code_never_imports_unittest(self):
        import subprocess, sys
        probe = ("import sys, pipeline.cli, pipeline.release, pipeline.orchestrate, "
                 "pipeline.harden; print('unittest' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                             cwd=coder.ROOT, env={k: v for k, v in os.environ.items()
                                                  if k != "ARENA_NATIVE_BACKEND"})
        self.assertEqual(out.stdout.strip(), "False", out.stderr[-500:])

    def test_missing_configuration_refuses_rather_than_running_locally(self):
        with patch.dict(os.environ, {"ARENA_NATIVE_BACKEND": "coder"}), \
                patch.object(coder, "settings", return_value=None), patch.object(coder, "_POOL", None):
            with self.assertRaises(ValueError) as caught:
                coder.pool()
        self.assertIn("only on Coder", str(caught.exception))


class Configuration(unittest.TestCase):
    def test_unknown_keys_and_bad_values_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workspace.toml"
            path.write_text('[coder]\ntemplate = "t"\ntypo_key = 1\n')
            with self.assertRaises(ValueError):
                coder.settings(path)
            path.write_text('[coder]\ntemplate = "t"\n')
            problems = coder.settings(path).problems()
        self.assertTrue(any("copy_parameters_from" in p for p in problems))
        # The multi-workspace keys are gone; a stale config says so instead of scaling.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workspace.toml"
            path.write_text('[coder]\ntemplate = "t"\nmax_instances = 5\n')
            with self.assertRaises(ValueError):
                coder.settings(path)
        self.assertTrue(cfg(trials_per_task=30).problems())

    def test_no_deployment_detail_lives_in_the_repository(self):
        """Operator rule: Coder details stay in the gitignored workspace.toml."""
        import subprocess
        tracked = subprocess.run(["git", "ls-files", "*.md", "workspace.example.toml",
                                  "pipeline/benchmark/coder.py"],
                                 capture_output=True, text=True, cwd=coder.ROOT).stdout.split()
        real = coder.settings()
        if real is None:
            self.skipTest("no local [coder] configuration to compare against")
        secrets = [v for v in (real.url, real.template, real.copy_parameters_from) if v]
        for rel in tracked:
            text = (coder.ROOT / rel).read_text(errors="replace")
            for value in secrets:
                self.assertNotIn(value, text, f"{value!r} appears in {rel}")


class Login(unittest.TestCase):
    def test_logged_out_and_non_interactive_refuses_with_instructions(self):
        with patch.object(coder, "logged_in", return_value=False):
            with self.assertRaises(ValueError) as caught:
                coder.ensure_login(cfg(url="https://deploy.example.invalid"), interactive=False)
        self.assertIn("./arena coder login", str(caught.exception))

    def test_interactive_login_opens_the_auth_page_and_runs_coder_login(self):
        state = iter([False, True])
        with patch.object(coder, "logged_in", side_effect=lambda: next(state)), \
                patch.object(coder.webbrowser, "open") as browser, \
                patch.object(coder.subprocess, "call", return_value=0) as call, \
                patch.object(coder, "_binary", return_value="coder"), \
                patch("builtins.print"):
            coder.ensure_login(cfg(url="https://deploy.example.invalid/"), interactive=True)
        browser.assert_called_once_with("https://deploy.example.invalid/cli-auth")
        call.assert_called_once_with(["coder", "login", "https://deploy.example.invalid"])

    def test_already_logged_in_does_nothing(self):
        with patch.object(coder, "logged_in", return_value=True), \
                patch.object(coder.webbrowser, "open") as browser:
            coder.ensure_login(cfg())
        browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class ScreenUsesTheCalibrationSurface(unittest.TestCase):
    """The screen measures exactly as the calibration of record does, narrower."""

    def _run(self, prior=None):
        from pipeline import policy, screen
        calls = []

        def fake_calibrate(bundle, snapshot, root, model, n, workers, receipt):
            calls.append({"model": model, "n": n, "workers": workers, "receipt": receipt})
            return f"campaign-{model}", [], True
        with tempfile.TemporaryDirectory() as tmp, \
                patch("pipeline.release._calibrate_model", side_effect=fake_calibrate), \
                patch("pipeline.artifacts.freeze", return_value="sha"), \
                patch("pipeline.artifacts.bundle_lock"), \
                patch("pipeline.screen.current", return_value=prior), \
                patch("pipeline.screen.summarize", side_effect=lambda b, index, n, m: {
                    "artifact_sha256": "sha", "created_at": 0, "index": index}), \
                patch("pipeline.screen._write"):
            record = screen.run(Path(tmp) / "bundle", native_root=Path(tmp))
        return calls, record, policy

    def test_both_spec_models_run_the_screen_width_through_the_native_path(self):
        calls, record, policy = self._run()
        self.assertEqual(sorted(c["model"] for c in calls),
                         sorted(policy.resolve_calibration_models()))
        self.assertTrue(all(c["n"] == policy.SCREEN_EPISODES_PER_MODEL for c in calls))
        # Its own top-up receipt: never overwrites the calibration of record's.
        self.assertTrue(all(c["receipt"] == "screen-topup" for c in calls))
        self.assertEqual(len(record["index"]["campaigns"]), 2)

    def test_a_scored_screen_of_this_version_is_reused_not_rerun(self):
        prior = {"episodes_per_model": 5, "artifact_sha256": "sha",
                 "per_model": {"anthropic/claude-fable-5-1": {"campaign": "old-fable", "episodes_valid": 5},
                               "openai/gpt-6-astra": {"campaign": "old-astra", "episodes_valid": 5}},
                 "index": {"topups": {}}}
        calls, record, _ = self._run(prior)
        self.assertEqual(calls, [], "results are not re-run")
        self.assertEqual(record["index"]["campaigns"], ["old-fable", "old-astra"])

    def test_one_models_failure_keeps_the_others_campaign(self):
        from pipeline import screen
        written = []

        def fake_calibrate(bundle, snapshot, root, model, n, workers, receipt):
            if model == "gpt-6-astra":
                raise RuntimeError("pool unavailable")
            return "campaign-fable", [], True
        with tempfile.TemporaryDirectory() as tmp, \
                patch("pipeline.release._calibrate_model", side_effect=fake_calibrate), \
                patch("pipeline.artifacts.freeze", return_value="sha"), \
                patch("pipeline.artifacts.bundle_lock"), \
                patch("pipeline.screen.current", return_value=None), \
                patch("pipeline.screen.summarize", side_effect=lambda b, index, n, m: {
                    "artifact_sha256": "sha", "created_at": 0, "index": index}), \
                patch("pipeline.screen._write", side_effect=lambda b, r: written.append(r)):
            with self.assertRaisesRegex(RuntimeError, "finished campaigns were kept"):
                screen.run(Path(tmp) / "bundle", native_root=Path(tmp))
        self.assertEqual(written[0]["index"]["campaigns"], ["campaign-fable"])

    def test_a_campaign_whose_evidence_failed_is_not_reused(self):
        prior = {"episodes_per_model": 5, "artifact_sha256": "sha",
                 "per_model": {"anthropic/claude-fable-5-1": {"campaign": "old-fable",
                                                              "episodes_valid": 5,
                                                              "evidence_problems": ["native runner changed"]},
                               "openai/gpt-6-astra": {"campaign": "old-astra", "episodes_valid": 5,
                                                      "evidence_problems": []}},
                 "index": {"topups": {}}}
        calls, record, _ = self._run(prior)
        self.assertEqual([c["model"] for c in calls], ["claude-fable-5-1"])
        self.assertIn("old-astra", record["index"]["campaigns"])
        self.assertNotIn("old-fable", record["index"]["campaigns"])

    def test_only_the_missing_model_is_launched(self):
        prior = {"episodes_per_model": 5, "artifact_sha256": "sha",
                 "per_model": {"anthropic/claude-fable-5-1": {"campaign": "old-fable", "episodes_valid": 5}},
                 "index": {"topups": {}}}
        calls, _, _ = self._run(prior)
        self.assertEqual([c["model"] for c in calls], ["gpt-6-astra"])


class PauseAndReuse(unittest.TestCase):
    """Operator rules: pause the workspaces once scores are extracted; reuse them next time."""

    def stopped_names(self, calls):
        return [c.args[1] for c in calls if c.args and c.args[0] == "stop"]

    def test_the_pool_pauses_itself_once_every_campaign_has_released(self):
        p = FastPool(cfg(idle_stop_after=1))
        a, b = p.acquire(), p.acquire()
        with patch.object(coder, "coder", return_value=type("P", (), {"returncode": 0})()) as cli:
            p.release(a)
            time.sleep(1.3)
            self.assertEqual(self.stopped_names(cli.call_args_list), [])   # one still running
            p.release(b)
            time.sleep(1.5)
        self.assertEqual(self.stopped_names(cli.call_args_list), ["arena-calib-1"])
        self.assertEqual(p.instances[0].state, "stopped")

    def test_a_paused_workspace_is_restarted_not_replaced(self):
        p = FastPool(cfg())
        first = p.acquire()
        p.release(first)
        with patch.object(coder, "coder", return_value=type("P", (), {"returncode": 0})()):
            self.assertEqual(p.pause(), ["arena-calib-1"])
        again = p.acquire()
        self.assertEqual(again.name, "arena-calib-1")
        self.assertEqual(len(p.instances), 1)
        self.assertEqual(p.provisioned, ["arena-calib-1", "arena-calib-1"])

    def test_the_workspace_paused_by_an_earlier_run_is_restarted_not_created(self):
        seen = []

        def provision(inst, existing):
            seen.append((inst.name, existing))
        p = Pool(cfg())
        with patch.object(coder, "ensure_login"), \
                patch.object(Pool, "_rows", return_value={"arena-calib-1": {"status": "stopped"}}), \
                patch.object(coder.Instance, "provision", provision):
            self.assertEqual(p.acquire().name, "arena-calib-1")
        self.assertEqual(seen, [("arena-calib-1", {"status": "stopped"})])

    def test_new_work_waits_for_a_pause_in_progress_instead_of_creating(self):
        p = FastPool(cfg())
        p.release(p.acquire())
        p.instances[0].state = "stopping"
        got = []
        waiter = threading.Thread(target=lambda: got.append(p.acquire()))
        waiter.start()
        time.sleep(0.2)
        self.assertEqual(len(p.instances), 1)            # nothing new while stopping
        with p.cond:
            p.instances[0].state = "stopped"
            p.cond.notify_all()
        waiter.join(timeout=35)
        self.assertEqual(got[0].name, "arena-calib-1")


class PinnedWorkspaces(unittest.TestCase):
    """A named workspace may be shared with other work: use it, never stop it."""

    def test_a_shared_workspace_replaces_the_pools_own_and_never_overflows(self):
        p = FastPool(cfg(workspace="shared", tasks_per_instance=2))
        held = [p.acquire(), p.acquire()]
        self.assertEqual({i.name for i in held}, {"shared"})
        got = []
        waiter = threading.Thread(target=lambda: got.append(p.acquire()))
        waiter.start()
        time.sleep(0.2)
        self.assertEqual(got, [])                                  # full: wait, never spill over
        p.release(held[0])
        waiter.join(timeout=5)
        self.assertEqual(got[0].name, "shared")
        self.assertEqual(p.provisioned, ["shared"])

    def test_pausing_never_stops_a_pinned_workspace(self):
        p = FastPool(cfg(workspace="shared", idle_stop_after=1))
        with patch.object(coder, "coder", return_value=type("P", (), {"returncode": 0})()) as cli:
            p.release(p.acquire())
            time.sleep(1.5)                                        # the idle pause fires
            self.assertEqual(p.pause(), [])                        # and the exit pause
        self.assertNotIn("shared", [c.args[1] for c in cli.call_args_list if c.args[:1] == ("stop",)])
        self.assertEqual(p.instances[0].state, "ready")

    def test_a_missing_pinned_workspace_is_refused_never_created(self):
        inst = coder.Instance("shared", cfg(workspace="shared"), pinned=True)
        with patch.object(coder, "coder") as cli:
            with self.assertRaises(RuntimeError):
                inst.provision(None)
        self.assertFalse(any(c.args[:1] == ("create",) for c in cli.call_args_list))

    def test_stop_and_delete_only_ever_see_the_pools_own_workspaces(self):
        p = Pool(cfg(workspace="shared"))
        with patch.object(Pool, "_rows", return_value={"shared": {}, "arena-calib-1": {},
                                                       "arena-calib-2": {}, "other": {}}):
            self.assertEqual(set(p._existing()), {"arena-calib-1"})

    def test_a_shared_workspace_cannot_be_the_pools_own(self):
        self.assertTrue(cfg(workspace="arena-calib-1").problems())
        self.assertTrue(cfg(workspace="").problems())
        self.assertFalse(cfg(workspace="shared").problems())


class LiveRunRegressions(unittest.TestCase):
    """Found by the first live run on a real deployment."""

    def test_everything_the_policy_fingerprint_reads_is_synced(self):
        import inspect, re
        from pipeline import artifacts
        needed = set(re.findall(r'root / ["\']([^"\']+)["\']', inspect.getsource(artifacts.policy_identity)))
        missing = [n for n in needed if not any(n == s or n.startswith(s + "/") for s in coder.SYNC_PATHS)]
        self.assertEqual(missing, [])

    def test_exit_pause_waits_for_a_workspace_still_starting_and_stops_it(self):
        p = FastPool(cfg())
        p.release(p.acquire())
        p.instances[0].state = "provisioning"

        def finish():
            time.sleep(0.3)
            with p.cond:
                p.instances[0].state = "ready"
                p.cond.notify_all()
        threading.Thread(target=finish).start()
        with patch.object(coder, "coder", return_value=type("P", (), {"returncode": 0})()) as cli:
            paused = p.pause()
        self.assertEqual(paused, ["arena-calib-1"])
        self.assertEqual([c.args[:2] for c in cli.call_args_list], [("stop", "arena-calib-1")])
