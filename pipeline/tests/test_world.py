"""The environment is a world (author/world.py): generated, realistic data around the trace's code."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline import policy, screen
from pipeline.artifacts import atomic_json
from pipeline.author import agent_designer as author, creation, world
from pipeline.ledger import faithfulness

LIB_DIR = world.LIB.parent

GOOD = '''
from arena_world import *
w = World()
rng = w.stream("customers")
staff = w.people("customers", 300, same_name_pairs=3)
ids = id_codes(rng, "C-", 300, start=10200)
write_csv("crm/customers.csv", [{"customer_id": i, "name": p["display"], "email": email(rng, p)}
                                 for i, p in zip(ids, staff)], ["customer_id", "name", "email"])
orng = w.stream("orders")
counts = heavy_tail_split(orng, 3000, 300)
times = business_times(orng, 3000, "2024-01-02", "2024-06-28", utc_offset_hours=5.5)
orng.shuffle(times)
rows, k = [], 0
for cid, n in zip(ids, counts):
    for _ in range(n):
        rows.append({"order_id": f"SO-{100000 + k}", "customer_id": cid, "placed_at": iso(times[k]),
                     "amount": f"{lognormal_amount(orng, 42.0):.2f}",
                     "status": zipf_choice(orng, ["paid", "shipped", "refunded", "cancelled", "disputed"])})
        k += 1
rows.sort(key=lambda r: r["placed_at"])
write_csv("exports/orders.csv", rows, list(rows[0]))
'''

FAKE = '''
import arena_world, datetime
rng = arena_world.World().stream("x")
start = datetime.datetime(2024, 1, 1, 9, 0, 0)
rows = [{"customer_id": f"C{i % 100}", "name": f"User{i % 300}", "amount": rng.randint(10, 1000) * 1.0,
         "ts": (start + datetime.timedelta(minutes=5 * i)).isoformat(),
         "note": "Lorem ipsum dolor sit amet consectetur", "tier": ["gold", "silver", "bronze", "basic"][i % 4]}
        for i in range(2000)]
arena_world.write_csv("data/customers.csv", rows, list(rows[0]))
'''


def run_local(source: str, seed: int) -> dict[str, bytes]:
    """The generator run the way the container runs it, without Docker."""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "generate.py"
        script.write_text(source)
        (Path(tmp) / "arena_world.py").write_bytes(world.LIB.read_bytes())
        out = Path(tmp) / "out"
        env = {**os.environ, "ARENA_WORLD_SEED": str(seed), "ARENA_WORLD_OUT": str(out)}
        subprocess.run([sys.executable, "-s", str(script)], cwd=tmp, env=env, check=True, capture_output=True)
        return {p.relative_to(out).as_posix(): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()}


class Library(unittest.TestCase):
    def test_same_seed_same_bytes_other_seed_other_bytes(self):
        a, b, c = run_local(GOOD, 7), run_local(GOOD, 7), run_local(GOOD, 8)
        self.assertEqual(a, b)
        self.assertNotEqual(a["exports/orders.csv"], c["exports/orders.csv"])

    def test_streams_are_independent(self):
        sys.path.insert(0, str(LIB_DIR))
        try:
            import arena_world
        finally:
            sys.path.remove(str(LIB_DIR))
        w = arena_world.World(3)
        first = [w.stream("orders").random() for _ in range(3)]
        w.stream("customers").random()
        self.assertEqual(first, [arena_world.World(3).stream("orders").random() for _ in range(3)])
        rng = w.stream("x")
        self.assertEqual(sum(arena_world.heavy_tail_split(rng, 1000, 37)), 1000)
        self.assertEqual(arena_world.rotate_violations(["a", "b", "c"], 5), ["a", "b", "c", "a", "b"])
        times = arena_world.business_times(rng, 500, "2024-03-01", "2024-03-31", utc_offset_hours=-5)
        self.assertTrue(all(8 <= t.hour < 19 for t in times))
        self.assertEqual(times, sorted(times))
        self.assertIn(arena_world.number_text(rng, 1234.5, style="eu"), {"1.234,50"})

    def test_contacts_are_fictional_and_pass_the_privacy_scan(self):
        from pipeline.privacy import scan_text
        files = run_local(GOOD, 11)
        self.assertEqual(scan_text("crm/customers.csv", files["crm/customers.csv"].decode()), [])


class Profile(unittest.TestCase):
    def test_realistic_world_scores_clean(self):
        report = world.profile(run_local(GOOD, 5))
        self.assertEqual(report["artifacts"], [])
        self.assertEqual(report["records"], 3300)
        self.assertEqual(report["realism"], 100)

    def test_synthetic_tells_are_flagged(self):
        kinds = {a["kind"] for a in world.profile(run_local(FAKE, 5))["artifacts"]}
        self.assertTrue({"identical_child_counts", "numbered_names", "uniform_numeric", "counter_timestamps",
                         "repeated_filler", "flat_categories", "placeholder_text"} <= kinds, kinds)

    def test_logs_and_json_are_profiled(self):
        log = "".join(f"2024-05-01T10:{i // 60:02d}:{i % 60:02d} INFO job {i}\n" for i in range(120)).encode()
        report = world.profile({"logs/app.log": log, "x.json": json.dumps({"rows": [{"k": i} for i in range(5)]}).encode()})
        self.assertIn("counter_timestamps", {a["kind"] for a in report["artifacts"]})


class Requirement(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "environment/repo").mkdir(parents=True)
        self.spec = {"traps": [{"id": "t_a"}, {"id": "t_b"}], "quality": {}}

    def live(self, records=600, artifacts=()):
        data = b"a,b\n1,2\n"
        (self.root / "environment/repo/data.csv").write_bytes(data)
        world.record_run(self.root, {"name": "g", "target": "fixture", "seed": 1, "generator": "authoring/world/g/generate.py",
                                     "outputs": {"data.csv": hashlib.sha256(data).hexdigest()}, "records": records,
                                     "profile": {"artifacts": list(artifacts)}})

    def test_not_armed_asks_nothing(self):
        world.arm(self.root, False, "repair")
        self.assertEqual(world.requirement_gaps(self.spec, self.root), [])

    def test_armed_lists_world_and_record(self):
        world.arm(self.root, True, "fresh design")
        gaps = world.requirement_gaps(self.spec, self.root)
        self.assertTrue(any(g.startswith("ENVIRONMENT WORLD (0 generated)") for g in gaps))
        self.assertIn("quality.world.discovery_path", gaps)
        self.assertTrue(any("population_traps (0/" in g for g in gaps))

    def test_complete_world_has_no_gaps_and_artifacts_are_listed(self):
        world.arm(self.root, True, "fresh design")
        self.live(artifacts=[{"file": "data.csv", "column": "b", "kind": "flat_categories", "detail": "x"}])
        self.spec["quality"]["world"] = {
            "generators": ["g"], "discovery_path": "run the report on the exports", "why_long": "joins across files",
            "population_traps": [{"trap": "t_a", "members": "12 near misses", "shape": "one condition broken"},
                                 {"trap": "t_b", "members": "3 same-name pairs", "shape": "identity"}]}
        self.assertEqual([g for g in world.requirement_gaps(self.spec, self.root) if "realism" not in g], [])
        self.assertTrue(any("flat_categories" in g for g in world.requirement_gaps(self.spec, self.root)))

    def test_an_edited_output_is_no_longer_generated(self):
        world.arm(self.root, True, "fresh design")
        self.live()
        self.assertEqual(len(world.live_runs(self.root)), 1)
        (self.root / "environment/repo/data.csv").write_text("edited\n")
        self.assertEqual(world.live_runs(self.root), [])
        self.assertEqual(world.synthetic_files(self.root), [])

    def test_declined_with_reason(self):
        world.arm(self.root, True, "fresh design")
        self.spec["quality"]["world"] = {"declined": "x" * policy.WORLD_DECLINE_REASON_MIN}
        self.assertEqual(world.requirement_gaps(self.spec, self.root), [])

    def test_creation_inventory_carries_the_gaps(self):
        world.arm(self.root, True, "fresh design")
        missing = creation.inventory(self.spec, self.root)["missing_components"]
        self.assertTrue(any(m.startswith("ENVIRONMENT WORLD") for m in missing))


class Ledger(unittest.TestCase):
    def test_synthetic_leaves_the_share_denominator(self):
        counts = {"lifted": 9, "authored": 1, "synthetic": 90}
        self.assertEqual(faithfulness.share(counts), 0.9)
        with patch.object(policy, "SYNTHETIC_DATA_FIDELITY", "authored"):
            self.assertEqual(faithfulness.share(counts), 0.09)

    def test_tally_keeps_old_shape_without_a_world(self):
        counts, by_bytes = faithfulness.tally([{"origin": "lifted", "bytes": 5}])
        self.assertEqual(list(counts), ["lifted", "anonymized", "derived", "authored"])
        counts, _ = faithfulness.tally([{"origin": "synthetic", "bytes": 5}])
        self.assertEqual(counts["synthetic"], 1)


class DesignerTool(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "environment/repo").mkdir(parents=True)
        (self.root / "environment/repo/app.py").write_text("print('hi')\n")
        self.extraction = SimpleNamespace(session_id="synthetic", seed_index=0, source_root=self.root, moments=[])
        self.handlers = author._handlers(self.root, self.extraction, {}, self.root / "draft.json")

    def generate(self, source, **kw):
        files = run_local(source, kw.get("seed", 3))
        with patch.object(world, "run_generator", return_value={"files": files, "seconds": 0.1, "stdout": "", "stderr": ""}):
            return self.handlers["generate_world"](name="orders", generator=source,
                                                   grounded_in=["synthetic::msg3::exports/orders.csv"], **kw)

    def test_fixture_outputs_are_written_declared_and_recorded(self):
        result = self.generate(GOOD, seed=3, out_dir="data")
        self.assertEqual(result["file_count"], 2)
        self.assertTrue((self.root / "environment/repo/data/exports/orders.csv").is_file())
        authored = json.loads((self.root / "authoring/extraction/fixture-authored.json").read_text())["files"]
        self.assertEqual(authored["data/exports/orders.csv"]["origin"], "synthetic")
        self.assertTrue((self.root / "authoring/world/orders/generate.py").is_file())
        self.assertEqual({r["path"] for r in world.synthetic_files(self.root)},
                         {"data/exports/orders.csv", "data/crm/customers.csv"})
        self.assertEqual(set(faithfulness._synthetic_outputs(self.root)), {"data/exports/orders.csv", "data/crm/customers.csv"})

    def test_rerun_replaces_the_previous_outputs(self):
        self.generate(GOOD, seed=3, out_dir="data")
        self.generate(GOOD, seed=3, out_dir="world")
        self.assertFalse((self.root / "environment/repo/data/exports/orders.csv").exists())
        self.assertTrue((self.root / "environment/repo/world/exports/orders.csv").is_file())
        self.assertEqual(len((world.load(self.root) or {})["runs"]), 1)

    def test_suites_target_stages_hidden_draws_and_generator(self):
        self.generate(GOOD, seed=99, target="suites", ship_generator=True)
        draft = json.loads((self.root / "draft.json").read_text())
        self.assertIn("data/exports/orders.csv", draft["suites"])
        self.assertIn("world/orders/generate.py", draft["suites"])
        self.assertFalse((self.root / "environment/repo/exports").exists())

    def test_fixture_runs_must_name_their_grounding(self):
        with self.assertRaises(ValueError):
            self.handlers["generate_world"](name="orders", generator=GOOD, seed=1)

    def test_private_looking_output_is_refused(self):
        leaky = 'import arena_world\narena_world.write_text("c.csv", "name,email\\nA,alice.smith@gmail.com\\n")\n' + "#" * 40
        result = self.generate(leaky)
        self.assertIn("private-looking", result.get("error", ""))

    def test_library_description(self):
        text = self.handlers["generate_world"](name="x", describe_library=True)["library"]
        self.assertIn("rotate_violations", text)


class EpisodeLength(unittest.TestCase):
    def test_minutes_and_advisory(self):
        with tempfile.TemporaryDirectory() as tmp:
            trials = []
            for i, minutes in enumerate((4, 6, 8)):
                path = Path(tmp) / f"r{i}.json"
                path.write_text(json.dumps({"step_results": [{"step_name": "01-a", "agent_result": {"n_output_tokens": 1000 * minutes},
                                                              "agent_execution": {"started_at": "2026-10-30T10:00:00Z",
                                                                                  "finished_at": f"2026-10-30T10:0{minutes}:00Z"}}]}))
                trials.append({"valid": True, "result_file": str(path)})
            trials.append({"valid": False, "result_file": str(path)})
            lengths = screen.episode_lengths({"trials": trials})
        self.assertEqual(lengths["steps_measured"], 3)
        self.assertEqual(lengths["median_minutes"], 6.0)
        self.assertEqual(lengths["median_output_tokens"], 6000)
        advisories = screen.length_advisories({"m": {"episode_length": lengths}})
        self.assertEqual(len(advisories), 1)
        self.assertEqual(screen.length_advisories({"m": {"episode_length": {"median_minutes": 45.0}}}), [])


if __name__ == "__main__":
    unittest.main()
