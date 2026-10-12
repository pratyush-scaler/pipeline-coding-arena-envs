"""Regression controls for category quotas, novelty and publication identity."""
import json
from pathlib import Path
import tempfile
import threading
import os
import time
from contextlib import ExitStack
import unittest
from unittest.mock import patch

from category_batch import Controller, allocate, authentication_failure, provider_failure, excluded_families, external_qualification, ranked_candidates, receipt_problems, refresh_anthropic_credentials, source_key
from pipeline.artifacts import identity, atomic_json
from category_batch import ProviderAdmission, configure_provider_admission, step_count_problems


#: Four distinct target levels for quota tests; letters stay as session ids.
LEVEL = {"A": "F3", "B": "F2", "C": "F1", "D": "F0"}


def row(session, category, rank, seed=0):
    return {"session_id": session, "seed_index": seed, "category": category,
            "_rank": rank, "classification": {"survives": True}}


class CategoryBatchTests(unittest.TestCase):
    def test_eager_selected_authoring_does_not_wait_for_a_reserved_quota(self):
        controller = Controller.__new__(Controller)
        controller.lock = threading.RLock()
        controller.request = {'per_category': 1, 'selected_only': True, 'eager_selected': True}
        controller.task_workers = controller.classify_workers = controller.qualification_workers = 1
        ready, fresh = row('ready', 'F3', 1), row('fresh', 'F3', 2)
        controller.queue = [ready, fresh]
        controller.records = {source_key(ready): {'row': ready, 'stage': 'validated'}}
        controller.classified = {source_key(r): {'row': r} for r in controller.queue}
        controller.save = controller.import_prefetched = lambda: None
        author_started = threading.Event()
        def update(source, stage, **fields):
            with controller.lock:
                controller.records[source_key(source)] = {'row': source, 'stage': stage, **fields}
        def build(source):
            author_started.set()
            update(source, 'awaiting-hardening')
        def qualify(source):
            self.assertTrue(author_started.wait(2), 'Selected draft must start while qualification owns a quota slot')
            update(source, 'accepted')
        controller.update, controller.build, controller.qualify = update, build, qualify
        self.assertEqual(controller.drive(), 1)
        self.assertTrue(author_started.is_set())
        self.assertEqual(controller.records[source_key(fresh)]['stage'], 'awaiting-hardening')

    def test_parallel_publication_cannot_exceed_category_quota(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            controller = Controller.__new__(Controller)
            controller.lock = threading.RLock()
            controller.work, controller.out = root / 'work', root / 'out'
            controller.request = {'per_category': 1, 'models': ['one', 'two']}
            controller.policy, controller.provider_error = 'policy', None
            controller.records = {}
            sources = [row('first', 'F3', 1), row('second', 'F3', 2)]
            for source in sources:
                candidate = root / source['session_id']
                candidate.mkdir()
                (candidate / 'task.toml').write_text('')
                atomic_json(candidate / 'tests/checks.json', {})
                controller.records[source_key(source)] = {'row': source, 'stage': 'validated', 'candidate': str(candidate)}
            def update(source, stage, **fields):
                with controller.lock:
                    controller.records[source_key(source)].update(stage=stage, **fields)
            controller.update = update
            checking, finish = threading.Event(), threading.Event()
            def evaluate(bundle):
                checking.set()
                self.assertTrue(finish.wait(2))
                return {'artifact_sha256': identity(bundle), 'release_ready': True, 'status': 'accepted'}
            stack.enter_context(patch('pipeline.release.qualify', return_value={'release_ready': True, 'status': 'accepted'}))
            stack.enter_context(patch('pipeline.screen.current', return_value={'shippable': True, 'measurement_complete': True}))
            stack.enter_context(patch('pipeline.release.evaluate', side_effect=evaluate))
            stack.enter_context(patch('category_batch.receipt_problems', return_value=[]))
            worker = threading.Thread(target=controller.qualify, args=(sources[0],))
            worker.start()
            try:
                self.assertTrue(checking.wait(2))
                controller.qualify(sources[1])
                self.assertEqual(controller.records[source_key(sources[1])]['stage'], 'qualified-reserve')
            finally:
                finish.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(controller.counts()['F3'], 1)
            self.assertEqual(controller.publication_claims, {})

    def test_selected_only_does_not_launch_more_source_screening(self):
        controller = Controller.__new__(Controller)
        controller.lock = threading.RLock()
        controller.request = {'per_category': 1, 'selected_only': True}
        controller.task_workers = controller.classify_workers = controller.qualification_workers = 1
        selected, unseen = row('selected', 'F3', 1), row('unseen', 'F2', 2)
        controller.queue = [selected, unseen]
        controller.records = {}
        controller.classified = {source_key(selected): {'row': selected}}
        controller.save = controller.import_prefetched = lambda: None
        def update(source, stage, **fields):
            controller.records[source_key(source)] = {'row': source, 'stage': stage, **fields}
        controller.update = update
        controller.build = lambda source: update(source, 'validated')
        controller.qualify = lambda source: update(source, 'accepted')
        with patch.object(controller, 'classify') as classify:
            self.assertEqual(controller.drive(), 1)
        classify.assert_not_called()
        self.assertEqual(controller.records[source_key(selected)]['stage'], 'accepted')

    def test_restart_does_not_resume_an_automatic_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = row('retained', 'F3', 1)
            atomic_json(root / 'queue.json', {'rows': []})
            atomic_json(root / 'tasks/retained-0.json', {'row': source, 'stage': 'building',
                'candidate': str(root / 'candidate')})
            request = {'work': str(root), 'out': str(root / 'out'), 'policy_sha256': 'policy'}
            with patch('pipeline.artifacts.policy_identity', return_value='policy'), \
                    patch('pipeline.checkpoint.resumable', return_value={
                        'stage': 'authoring', 'feedback': ['Verified reviewer finding']}):
                controller = Controller(request, 1, 1)
            self.assertEqual(controller.records['retained-0']['stage'], 'awaiting-hardening')

    def _restart_with_parked_screen(self, current):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = row('parked', 'F3', 1)
            atomic_json(root / 'queue.json', {'rows': []})
            atomic_json(root / 'tasks/parked-0.json', {'row': source, 'stage': 'screen-needs-operator',
                'candidate': str(root / 'candidate')})
            request = {'work': str(root), 'out': str(root / 'out'), 'policy_sha256': 'policy'}
            with patch('pipeline.artifacts.policy_identity', return_value='policy'), \
                    patch('pipeline.screen.current', return_value=current):
                controller = Controller(request, 1, 1)
            return controller.records['parked-0']['stage']

    def test_a_parked_screen_rejoins_qualification_once_the_operator_relaunched_it(self):
        # Re-review: "screen-needs-operator" was never reset, so after the
        # operator's relaunch produced a clean screen the task stayed parked.
        self.assertEqual(self._restart_with_parked_screen(
            {"measurement_complete": True, "topup_exhausted": []}), 'validated')
        self.assertEqual(self._restart_with_parked_screen(None), 'validated',
                         "a changed task needs a fresh screen, which qualification runs")

    def test_a_parked_screen_stays_parked_until_the_operator_acts(self):
        self.assertEqual(self._restart_with_parked_screen(
            {"measurement_complete": False, "topup_exhausted": ["anthropic/claude-fable-5-1"]}),
            'screen-needs-operator')

    def test_busy_author_provider_does_not_block_independent_review(self):
        admission = ProviderAdmission({'anthropic': 1, 'openai': 1})
        with admission.slot():
            with admission.slot('openai'):
                state = admission.stats()
                self.assertEqual(state['requests'], 2)
                self.assertEqual(state['providers']['anthropic']['active'], 1)
                self.assertEqual(state['providers']['openai']['active'], 1)
        self.assertEqual(admission.stats()['providers']['openai']['active'], 0)

    def test_admission_is_fifo_and_shared_budget_is_not_reset(self):
        admission = ProviderAdmission({'anthropic': 1, 'openai': 1}, max_requests=4, spent_requests=1)
        admitted, errors, threads = [], [], []
        def caller(number):
            try:
                with admission.slot():
                    admitted.append(number)
            except Exception as exc:
                errors.append(exc)
        with admission.slot():
            for number in range(2):
                thread = threading.Thread(target=caller, args=(number,))
                thread.start()
                threads.append(thread)
                with admission.condition:
                    self.assertTrue(admission.condition.wait_for(
                        lambda: len(admission.queues['anthropic']) == number + 1, timeout=2))
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(admitted, [0, 1])
        self.assertEqual(admission.stats()['requests'], 4)
        from pipeline.provider import ProviderBudgetExhausted
        with self.assertRaisesRegex(ProviderBudgetExhausted, 'budget exhausted'):
            with admission.slot('openai'):
                self.fail('Providers must share the request budget')
        self.assertEqual(admission.stats()['providers']['openai']['waiting'], 0)

    def test_transport_failure_releases_slot_and_waiters_have_no_wall_deadline(self):
        # Calibration runs with no time limits. A waiter queued past any former
        # deadline is admitted when the slot frees, never voided.
        admission = ProviderAdmission({'anthropic': 1, 'openai': 1})
        with self.assertRaisesRegex(ValueError, 'transport'):
            with admission.slot():
                raise ValueError('transport')
        self.assertEqual(admission.stats()['providers']['anthropic']['active'], 0)
        admitted, errors = [], []
        def blocked():
            try:
                with admission.slot():
                    admitted.append(True)
            except BaseException as exc:
                errors.append(str(exc))
        with admission.slot():
            thread = threading.Thread(target=blocked)
            thread.start()
            with admission.condition:
                self.assertTrue(admission.condition.wait_for(
                    lambda: bool(admission.queues['anthropic']), timeout=2))
                admission.started = time.monotonic() - 10 ** 6
                admission.condition.notify_all()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(admitted, [True])
        self.assertNotIn('wall_seconds', admission.stats())

    def test_all_in_process_transports_use_the_correct_provider_queue(self):
        from pipeline import provider
        from pipeline.author import transport as designer
        from pipeline.calibrate import agent, agent_openai
        from pipeline.select import classify
        with ExitStack() as stack:
            stack.enter_context(patch.object(provider, 'GLOBAL'))
            for module in (provider, designer, agent, agent_openai, classify):
                stack.enter_context(patch.object(module, 'request_slot'))
            admission = configure_provider_admission(4, 3, {'requests': 7, 'elapsed_seconds': 20})
            with ExitStack() as calls:
                for module in (provider, designer, agent, classify, agent_openai):
                    calls.enter_context(module.request_slot())
                state = admission.stats()
                self.assertEqual(state['providers']['anthropic']['active'], 4)
                self.assertEqual(state['providers']['openai']['active'], 1)
                self.assertEqual(state['requests'], 12)
                self.assertGreaterEqual(state['elapsed_seconds'], 20)

    def test_batch_requires_two_compiled_steps_without_mutating_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            path = bundle / 'authoring/spec.json'
            spec = {'instruction': 'First decision', 'second_step': {'instruction': 'Revised decision'},
                    'outputs': [{'file': 'first.json', 'step': 1}]}
            atomic_json(path, spec)
            before = path.read_bytes()
            self.assertEqual(step_count_problems(bundle, 2), [])
            self.assertEqual(path.read_bytes(), before)
            for count in (1, 2, 3):
                atomic_json(path, {'steps': [{'name': f'step-{i}', 'instruction': 'Decide'} for i in range(count)]})
                self.assertEqual(bool(step_count_problems(bundle, 2)), count != 2)
            path.unlink()
            self.assertIn('Cannot verify', step_count_problems(bundle, 2)[0])

    def test_single_step_candidate_cannot_start_native_qualification(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp)
            atomic_json(candidate / 'authoring/spec.json', {'instruction': 'One step only'})
            controller = Controller.__new__(Controller)
            controller.work = candidate
            controller.request = {'required_steps': 2}
            source = row('source', 'F3', 1)
            controller.records = {source_key(source): {'stage': 'validated', 'candidate': str(candidate)}}
            with patch.object(controller, 'update') as update, patch('pipeline.release.qualify') as qualify:
                controller.qualify(source)
                qualify.assert_not_called()
                self.assertEqual(update.call_args.args[1], 'qualification-rejected')
                self.assertIn('exactly 2 steps', update.call_args.kwargs['errors'][0])

    def test_transport_failures_pause_admission_instead_of_rejecting_sources(self):
        controller = Controller.__new__(Controller)
        controller.lock = threading.RLock()
        for error in ('Anthropic retry delay exceeds the remaining call deadline',
                      'Anthropic call deadline expired while awaiting admission',
                      'Anthropic transport interrupted (TimeoutError)',
                      'OpenAI transport interrupted (ConnectionError)'):
            with self.subTest(error=error):
                self.assertTrue(provider_failure(error))
                controller.note_provider_failure(error)
                self.assertEqual(controller.provider_error, 'provider transport unavailable')
        self.assertFalse(provider_failure('The task instruction omits the source deadline'))

    def test_recovered_author_does_not_report_a_historical_error_as_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = Controller.__new__(Controller)
            controller.work = Path(tmp)
            controller.lock = threading.RLock()
            source = row('source', 'F3', 1)
            controller.records = {source_key(source): {'row': source, 'stage': 'error',
                'error': 'old transport interruption', 'build': {'status': 'error'},
                'gate_repairs_remaining': 0}}
            with patch.object(controller, 'save'), patch('builtins.print'):
                controller.update(source, 'building')
            current = controller.records[source_key(source)]
            self.assertNotIn('error', current)
            self.assertNotIn('build', current)
            self.assertEqual(current['prior_outcomes'][0]['error'], 'old transport interruption')
            self.assertEqual(current['gate_repairs_remaining'], 0)


    def test_key_rotation_replaces_stale_inherited_authentication(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "ANTHROPIC_AUTH_TOKEN": "stale-token", "ANTHROPIC_API_KEY": "stale-key"}):
            path = Path(tmp) / "keys.env"
            path.write_text('ANTHROPIC_API_KEY="new-key"\n')
            refresh_anthropic_credentials(path)
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "new-key")
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", os.environ)
            path.write_text('ANTHROPIC_AUTH_TOKEN="new-token"\n')
            refresh_anthropic_credentials(path)
            self.assertEqual(os.environ["ANTHROPIC_AUTH_TOKEN"], "new-token")
            self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
            path.write_text("")
            with self.assertRaises(RuntimeError):
                refresh_anthropic_credentials(path)
            self.assertEqual(os.environ["ANTHROPIC_AUTH_TOKEN"], "new-token")



    def test_authentication_failure_stops_admission_before_other_sources_are_used(self):
        controller = Controller.__new__(Controller)
        controller.lock = threading.RLock()
        controller.request = {"per_category": 10}
        controller.task_workers = controller.classify_workers = 1
        controller.queue = [row(str(i), "F3", i) for i in range(10)]
        controller.classified = {str(i): {"row": source} for i, source in enumerate(controller.queue)}
        controller.records = {}
        controller.save = lambda: None
        controller.import_prefetched = lambda: None
        attempted = []
        def update(source, stage, **fields):
            controller.records[source['session_id']] = {'row': source, 'stage': stage}
        def build(source):
            attempted.append(source['session_id'])
            controller.note_provider_failure("RuntimeError: HTTP 401: provider request failed")
            update(source, 'provider-unavailable')
        controller.update, controller.build = update, build
        self.assertEqual(controller.drive(), 2)
        self.assertEqual(len(attempted), 1)
        self.assertEqual(controller.phase, 'provider-auth-unavailable')
        self.assertFalse(authentication_failure('HTTP 429: provider request failed'))
        self.assertFalse(authentication_failure('task validation failed'))

    def test_rate_limit_or_exhausted_budget_stops_new_candidate_admission(self):
        for error in ('HTTP 429: provider request failed',
                      'provider request or wall-clock budget exhausted; evidence remains unverified'):
            with self.subTest(error=error):
                controller = Controller.__new__(Controller)
                controller.lock = threading.RLock()
                controller.request = {'per_category': 10}
                controller.task_workers = controller.classify_workers = 1
                controller.queue = [row(str(i), 'F3', i) for i in range(10)]
                controller.classified = {source_key(r): {'row': r} for r in controller.queue}
                controller.records = {}
                controller.save = controller.import_prefetched = lambda: None
                attempted = []
                def update(source, stage, **fields):
                    controller.records[source_key(source)] = {'row': source, 'stage': stage}
                def build(source):
                    attempted.append(source['session_id'])
                    controller.note_provider_failure(error)
                    update(source, 'provider-unavailable')
                controller.update, controller.build = update, build
                self.assertEqual(controller.drive(), 2)
                self.assertEqual(len(attempted), 1)
                self.assertEqual(controller.phase, 'provider-unavailable')
        self.assertFalse(provider_failure('semantic review requested revision'))

    def test_resume_passes_consumed_gate_budget_and_preserves_workspace(self):
        from pipeline.orchestrate import TaskResult
        with tempfile.TemporaryDirectory() as tmp:
            controller = Controller.__new__(Controller)
            controller.work = Path(tmp)
            controller.budget = None
            source = row('new', 'F3', 1)
            controller.records = {source_key(source): {'stage': 'resume-session',
                'authoring_session': {'remaining_steps': 23, 'response_count': 117},
                'gate_repairs_remaining': 0}}
            from pipeline.checkpoint import update
            update(Path(tmp) / 'candidates/target-F3-replay/c-001-task', stage='authoring',
                   authoring_session={'remaining_steps': 23, 'response_count': 117},
                   gate_repairs_remaining=0)
            controller.update = lambda *args, **kwargs: None
            result = TaskResult(slug='draft', session_id='new', seed_index=0, tier=1, status='validated')
            with patch('pipeline.author.agent_designer.restore_design_session', create=True) as restore, \
                 patch('pipeline.orchestrate.build_one', return_value=result) as build:
                controller.build(source)
                restore.assert_called_once()
                self.assertEqual(build.call_args.kwargs, {'gate_repairs': 0, 'preserve_workspace': True})

    def test_the_designer_accepts_what_the_build_passes_and_keeps_the_first_mandate(self):
        # Re-review: `designer(source, extraction, bundle, feedback=None)` was
        # called with `budget=` by every build, so no build here could design
        # (TypeError since 2c38661). And the first design always carries the
        # DESIGN_BRIEF as feedback, which used to select the REVISION mandate.
        from pipeline.orchestrate import TaskResult
        from category_batch import DESIGN_BRIEF
        with tempfile.TemporaryDirectory() as tmp:
            controller = Controller.__new__(Controller)
            controller.work = Path(tmp)
            controller.budget = None
            source = row('new', 'F3', 1)
            controller.records = {}
            controller.update = lambda *args, **kwargs: None
            result = TaskResult(slug='draft', session_id='new', seed_index=0, tier=1, status='validated')
            with patch('pipeline.orchestrate.build_one', return_value=result) as build:
                controller.build(source)
            designer = build.call_args.args[3]
            bundle = Path(tmp) / 'bundle'
            bundle.mkdir()
            with patch('pipeline.author.agent_designer.design', return_value={}) as design:
                designer(source, object(), bundle, budget=None, feedback=None,
                         initial_spec=None, revision=False)
                first = design.call_args.kwargs
                designer(source, object(), bundle, budget=None, feedback=['gate finding'],
                         initial_spec={}, revision=True)
                repair = design.call_args.kwargs
                designer(source, object(), bundle, budget=None, feedback=['continue the stalled draft'],
                         initial_spec={}, revision=False)
                stalled = design.call_args.kwargs
        self.assertIs(first['revision'], False)
        self.assertEqual(first['feedback'][:len(DESIGN_BRIEF)], DESIGN_BRIEF)
        self.assertIs(repair['revision'], True)
        self.assertIs(stalled['revision'], False, "a stalled first design is still the first design")

    def test_the_design_brief_states_the_enforced_band_limits(self):
        from category_batch import DESIGN_BRIEF
        from pipeline.author.quality import BAND_LIMITS
        text = " ".join(DESIGN_BRIEF)
        core, stretch = BAND_LIMITS['core'], BAND_LIMITS['stretch']
        self.assertIn(f"{core[0]}–{core[1]} independent consequential core decisions", text)
        self.assertIn(f"{stretch[0]}–{stretch[1]} stretch decisions", text)
        self.assertNotIn("1–2 stretch", text)

    def test_retained_qualification_is_joined_without_duplicate_model_campaigns(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            receipt = {"status": "blocked", "release_ready": False}
            atomic_json(work / "finished.json", receipt)
            self.assertEqual(external_qualification(work, {"result": "finished.json"}), receipt)
            with self.assertRaisesRegex(RuntimeError, "original deadline"):
                external_qualification(work, {"result": "missing.json", "deadline": 0})
            controller = Controller.__new__(Controller)
            controller.work = controller.out = work
            controller.policy = "policy"
            controller.request = {"models": ["one", "two"]}
            source = row("source", "F1", 1)
            controller.records = {"source-0": {"stage": "qualifying-external",
                "external_qualification": {"result": "finished.json"}}}
            controller.update = lambda *args, **kwargs: None
            with patch("pipeline.release.qualify") as qualify, \
                 patch("category_batch.receipt_problems", return_value=["not accepted"]):
                controller.qualify(source)
                qualify.assert_not_called()

    def test_runtime_recovery_does_not_reset_a_consumed_repair_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = Controller.__new__(Controller)
            controller.work = controller.out = Path(tmp)
            controller.policy = "policy"
            controller.request = {"models": ["one", "two"]}
            source = row("source", "F1", 1)
            controller.records = {"source-0": {"stage": "qualifying", "qualification_repairs_remaining": 0}}
            controller.update = lambda *args, **kwargs: None
            with patch("pipeline.release.qualify", return_value={"status": "blocked"}) as qualify, \
                 patch("pipeline.screen.current", return_value={"shippable": True, "measurement_complete": True}), \
                 patch("category_batch.receipt_problems", return_value=["not accepted"]):
                controller.qualify(source)
                self.assertEqual(qualify.call_args.kwargs["repair_attempts"], 0)
                controller.records["source-0"]["qualification_repairs_remaining"] = 2
                qualify.reset_mock()
                controller.qualify(source)
                qualify.assert_not_called()

    def _qualify_with_screen(self, screened):
        with tempfile.TemporaryDirectory() as tmp:
            controller = Controller.__new__(Controller)
            controller.work = controller.out = Path(tmp)
            controller.policy, controller.provider_error = "policy", None
            controller.request = {"models": ["one", "two"]}
            source = row("source", "F1", 1)
            controller.records = {"source-0": {"stage": "validated", "candidate": str(Path(tmp) / "c")}}
            updates = []
            controller.update = lambda src, stage, **fields: updates.append((stage, fields))
            with patch("pipeline.release.qualify", return_value={"status": "blocked"}) as qualify, \
                 patch("pipeline.screen.current", return_value=None), \
                 patch("pipeline.screen.run", return_value=screened) as run, \
                 patch("category_batch.receipt_problems", return_value=["not accepted"]), \
                 patch("category_batch.completed_calibration", return_value=None), \
                 patch("category_batch.step_count_problems", return_value=[]):
                controller.qualify(source)
            run.assert_called_once()
            return qualify, updates

    def test_an_easy_task_is_never_held_for_its_pass_at_1(self):
        # User, 2026-09-25: pass@1 > 0.5 is not a blocker; the task proceeds.
        qualify, _ = self._qualify_with_screen({
            "shippable": True, "measurement_complete": True, "meets_target": False,
            "target_gap": ["pass@1 is not below 0.5 on both models"]})
        qualify.assert_called_once()

    def test_a_screen_that_did_not_measure_cleanly_holds_the_task(self):
        qualify, updates = self._qualify_with_screen({
            "shippable": False, "outlook": {"verdict": "not-assessable"},
            "shippable_problems": ["one: 2 valid episodes of 5 after 3 void(s)"],
            "per_model": {"one": {"pass_at_1": 1.0, "pass_at_4": None, "mean_reward": 1.0}}})
        qualify.assert_not_called()
        self.assertEqual(updates[-1][0], "screen-incomplete")

    def test_an_incomplete_screen_is_retried_once_per_controller_run(self):
        # Review: an unclean screen marked the row "unverified", and a restart
        # never re-screened a built bundle. It is retried once per run -- never
        # in a loop within one run (screen-incomplete is not a qualify stage).
        import category_batch
        self.assertNotIn("screen-incomplete", category_batch.QUALIFICATION_STAGES)
        source = Path(category_batch.__file__).read_text()
        self.assertIn('if record.get("stage") == "screen-incomplete":\n                record["stage"] = "validated"',
                      source)

    def test_native_queue_reserves_quota_without_occupying_author_worker(self):
        controller = Controller.__new__(Controller)
        controller.lock = threading.RLock()
        controller.request = {"per_category": 2}
        controller.task_workers = controller.classify_workers = controller.qualification_workers = 1
        controller.queue = [row(f'{c}{i}', LEVEL[c], i) for c in 'ABCD' for i in range(1, 5)]
        controller.classified = {source_key(r): {"row": r} for r in controller.queue}
        first = controller.queue[0]
        controller.records = {source_key(first): {'row': first, 'stage': 'validated'}}
        controller.save = controller.import_prefetched = lambda: None
        controller.finalize = lambda: True
        another_author_started = threading.Event()
        native_started = threading.Event()
        built, qualified = [], []

        def update(source, stage, **fields):
            with controller.lock:
                controller.records[source_key(source)] = {'row': source, 'stage': stage, **fields}

        def build(source):
            self.assertTrue(native_started.wait(2))
            built.append(source['session_id'])
            another_author_started.set()
            update(source, 'validated')

        def qualify(source):
            native_started.set()
            self.assertTrue(another_author_started.wait(2), 'Native work must not occupy the author slot')
            qualified.append(source['session_id'])
            update(source, 'accepted')

        controller.update, controller.build, controller.qualify = update, build, qualify
        self.assertEqual(controller.drive(), 0)
        self.assertEqual(len(built), 7)
        self.assertEqual(len(qualified), 8)
        self.assertEqual(dict(controller.counts()), dict.fromkeys(LEVEL.values(), 2))
        self.assertEqual(len(set(qualified)), 8)

    def test_recovered_attempts_cannot_starve_other_categories(self):
        controller = Controller.__new__(Controller)
        controller.lock = threading.RLock()
        controller.request = {"per_category": 2}
        controller.task_workers, controller.classify_workers = 4, 1
        controller.queue = []
        controller.classified = {}
        controller.records = {}
        for category in ('F3', 'F2', 'F1', 'F0'):
            for i in range(2):
                source = row(f'{category}{i}', category, len(controller.records))
                controller.records[source_key(source)] = {'row': source, 'stage': 'resume-session',
                    'authoring_session': {'response_count': 100 if category == 'F3' else 0}}
        controller.save = controller.import_prefetched = lambda: None
        controller.finalize = lambda: True
        barrier = threading.Barrier(4)
        first_categories = []

        def build(source):
            with controller.lock:
                first_wave = len(first_categories) < 4
                if first_wave:
                    first_categories.append(source['category'])
            if first_wave:
                barrier.wait(timeout=3)
            with controller.lock:
                controller.records[source_key(source)]['stage'] = 'accepted'

        controller.build = build
        self.assertEqual(controller.drive(), 0)
        self.assertEqual(sorted(first_categories), ['F0', 'F1', 'F2', 'F3'])

    def test_authoring_stops_at_validated_without_starting_native_campaign(self):
        from pipeline.orchestrate import TaskResult
        with tempfile.TemporaryDirectory() as tmp:
            controller = Controller.__new__(Controller)
            controller.work = Path(tmp)
            controller.budget = None
            controller.records = {}
            source = row('new', 'F3', 1)
            stages = []
            controller.update = lambda source, stage, **fields: stages.append(stage)
            result = TaskResult(slug='draft', session_id='new', seed_index=0, tier=1, status='validated')
            with patch('pipeline.orchestrate.build_one', return_value=result), \
                 patch('pipeline.release.qualify') as qualify:
                controller.build(source)
            self.assertEqual(stages, ['building', 'validated'])
            qualify.assert_not_called()


    def test_screening_and_other_builds_continue_while_first_author_is_busy(self):
        controller = Controller.__new__(Controller)
        controller.lock = threading.RLock()
        controller.request = {"per_category": 1}
        controller.task_workers, controller.classify_workers = 2, 1
        controller.queue = [row(c, LEVEL[c], i + 1) for i, c in enumerate("ABCD")]
        controller.classified = {"A-0": {"row": controller.queue[0]}}
        controller.records = {}
        controller.save = lambda: None
        controller.import_prefetched = lambda: None
        controller.finalize = lambda: True
        second_started = threading.Event()
        built = []

        def update(source, stage, **fields):
            with controller.lock:
                controller.records[source["session_id"]] = {"row": source, "stage": stage}

        def classify(source):
            result = {"row": source}
            with controller.lock:
                controller.classified[source["session_id"] + "-0"] = result
            return result

        def build(source):
            if source["category"] == "F3":
                self.assertTrue(second_started.wait(2), "Another author must start before the first finishes")
            else:
                second_started.set()
            with controller.lock:
                built.append(source["session_id"])
                update(source, "accepted")

        controller.update, controller.classify, controller.build = update, classify, build
        self.assertEqual(controller.drive(), 0)
        self.assertEqual(sorted(built), list("ABCD"))
        self.assertEqual(controller.phase, "complete")

    def test_prefetch_is_imported_with_schema_source_and_policy_checks(self):
        from pipeline.dump.evidence import SCHEMA
        with tempfile.TemporaryDirectory() as tmp:
            controller = Controller.__new__(Controller)
            controller.work, controller.policy = Path(tmp), "policy"
            controller.lock = threading.RLock()
            original = row("C", "F1", 1)
            original.pop("classification")
            original["source_evidence_sha256"] = "source"
            controller.queue, controller.classified = [original], {}
            atomic_json(controller.work / "prefetch/request.json", {
                "policy_sha256": "policy", "rows": [original]})
            result = {"row": original, "classification": {"survives": True}, "verdict": {
                "moments": [], "defect_pairs": [], "failure_mode": "", "repo_task": None,
                "reproduction": None, "truth_source": "none", "runtime_feasible": False,
                "deliverable_kind": "none", "coding_request": False,
                "supporting_evidence": [], "domain": "none", "gradable": False,
                "why_not": "nothing a test could run", "evidence_schema": SCHEMA,
                "source_evidence_sha256": "source"}}
            atomic_json(controller.work / "prefetch/results/C-0.json", result)
            controller.import_prefetched()
            self.assertFalse(controller.classified["C-0"]["row"]["classification"]["survives"])
            self.assertTrue(controller.classified["C-0"]["prefetched"])
            controller.classified.clear()
            result["verdict"]["source_evidence_sha256"] = "changed"
            atomic_json(controller.work / "prefetch/results/C-0.json", result)
            with self.assertRaisesRegex(ValueError, "stale source"):
                controller.import_prefetched()
            atomic_json(controller.work / "prefetch/request.json", {
                "policy_sha256": "changed", "rows": [original]})
            with self.assertRaisesRegex(ValueError, "another policy"):
                controller.import_prefetched()


    def test_scarce_category_gets_its_only_source(self):
        rows = [row("shared", "F3", 1), row("a-only", "F3", 2), row("shared", "F2", 3)]
        selected = allocate(rows, {"F3": 1, "F2": 1, "F1": 0, "F0": 0}, set())
        self.assertEqual({(r["category"], r["session_id"]) for r in selected},
                         {("F3", "a-only"), ("F2", "shared")})

    def test_alternating_path_allocates_all_four_categories(self):
        rows = [row("ab", "F3", 1), row("bc", "F2", 2), row("cd", "F1", 3),
                row("cd", "F0", 4), row("bc", "F1", 5), row("ab", "F2", 6), row("a", "F3", 7)]
        selected = allocate(rows, dict.fromkeys(("F3", "F2", "F1", "F0"), 1), set())
        self.assertEqual(len(selected), 4)
        self.assertEqual(len({r["session_id"] for r in selected}), 4)

    def test_quota_shortfall_is_not_backfilled_or_duplicated(self):
        rows = [row("one", "F3", 1), row("one", "F3", 2, 1), row("two", "F3", 3)]
        selected = allocate(rows, dict.fromkeys(("F3", "F2", "F1", "F0"), 1), set())
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["category"], "F3")

    def test_rejected_and_previously_used_sources_never_fill_quota(self):
        rejected = row("rejected", "F2", 2)
        rejected["classification"]["survives"] = False
        selected = allocate([row("old", "F3", 1), rejected], dict.fromkeys(("F3", "F2", "F1", "F0"), 1), {"old"})
        self.assertEqual(selected, [])

    def test_ranking_rederives_categories_and_excludes_complete_family(self):
        rows = [dict(row("old", "F3", 1), tier=1, replay_readiness=.9, corrections=3),
                dict(row("new", "F3", 2), tier=4, replay_readiness=.2, corrections=0)]
        selected = ranked_candidates(rows, {"old"})
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["category"], "F0")
        self.assertEqual(selected[0]["_rank"], 1)

    def test_legacy_reference_families_are_excluded_by_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "kw-007-legacy"
            bundle.mkdir()
            (bundle / "task.toml").write_text("")
            atomic_json(bundle / "provenance/ledger.json", {"source": {"session_id": "prior"}})
            self.assertEqual(set(excluded_families([root])), {"prior"})

    def test_unidentifiable_existing_task_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "c-001-unidentified").mkdir()
            with self.assertRaisesRegex(ValueError, "source family"):
                excluded_families([root])

    def test_only_current_accepted_receipts_can_be_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "task.toml").write_text("")
            atomic_json(root / "tests/checks.json", {})
            receipt = {"status": "accepted", "release_ready": True,
                       "artifact_sha256": identity(root), "policy_sha256": "policy"}
            self.assertEqual(receipt_problems(root, receipt, "policy"), [])
            atomic_json(root / "verification/batch-selection.json", {"category": "F3"})
            self.assertEqual(receipt_problems(root, receipt, "policy"), [])
            for status in ("validated", "error", "unverified", "blocked"):
                self.assertTrue(receipt_problems(root, dict(receipt, status=status), "policy"))
            self.assertTrue(receipt_problems(root, receipt, "changed-policy"))
            (root / "task.toml").write_text("changed")
            self.assertTrue(receipt_problems(root, receipt, "policy"))

    def test_final_batch_rechecks_every_task_against_complete_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            controller = Controller.__new__(Controller)
            controller.work, controller.policy, controller.task_workers = work, "policy", 2
            controller.request = {"models": ["author", "other"]}
            controller.save = lambda: None
            controller.update = lambda *args, **kwargs: None
            controller.records = {}
            for index, category in enumerate("ABCD"):
                bundle = work / category
                bundle.mkdir()
                (bundle / "task.toml").write_text("")
                atomic_json(bundle / "tests/checks.json", {"category": category})
                controller.records[category] = {"row": row(category, category, index),
                    "stage": "accepted", "bundle": str(bundle)}

            def accepted(bundle, *args, **kwargs):
                return {"status": "accepted", "release_ready": True,
                        "artifact_sha256": identity(bundle), "policy_sha256": "policy"}

            with patch("pipeline.release.qualify", side_effect=accepted) as qualify, \
                 patch("pipeline.release.evaluate", side_effect=accepted) as evaluate:
                self.assertTrue(controller.finalize())
                self.assertEqual(qualify.call_count, 4)
                self.assertEqual(evaluate.call_count, 4)
                self.assertTrue(all(c.kwargs["repair_attempts"] == 0 for c in qualify.call_args_list))

            def reject_one(bundle, *args, **kwargs):
                result = accepted(bundle)
                if bundle.name == "B":
                    result.update(status="blocked", release_ready=False)
                return result

            with patch("pipeline.release.qualify", side_effect=reject_one), \
                 patch("pipeline.release.evaluate") as evaluate:
                self.assertFalse(controller.finalize())
                self.assertFalse((work / "B").exists())
                self.assertTrue((work / "batch-review-rejected/B-0").is_dir())
                evaluate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
