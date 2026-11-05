"""Offline ten-minute scheduling, persistence and exact-display safety tests."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from agent_captain.core.auto_reconnect import AutoReconnect, INTERVAL_SECONDS
from agent_captain.infrastructure.store import Store


TERMINAL = 'ceohub-0123456789abcdef0123456789abcdef'
OTHER = 'ceohub-abcdef0123456789abcdef0123456789'
ROUTE_MESSAGE = 'SSH 路由暂不可达；请检查机器或恢复 SSH 上游/隧道路由，再由用户重试。不会自动重启服务。'


class Clock:
    now = 1000

    def __call__(self):
        return self.now


class Terminals:
    def __init__(self):
        self.records = {TERMINAL: {'id': TERMINAL, 'connected': False}}
        self.queries = []
        self.error = None

    def request(self, path, **kwargs):
        assert path == '/sessions'
        self.queries.append(path)
        if self.error:
            raise self.error
        return {'sessions': list(self.records.values())}


class Hub:
    def __init__(self, path):
        self.store = Store(path)
        self.registry_lock = threading.RLock()
        self.stopping = False
        self.terminals = Terminals()
        self.hosts = {'remote': {'transport': 'ssh'}, 'local': {'transport': 'local'}}
        self.store.remember('remote', 'worker', 'Work', mode='terminal', terminal_id=TERMINAL, cwd='/work')
        self.recoveries = {}
        self.calls = []
        self.answer = None
        self.callback = None
        self.human_busy = False

    def host(self, host):
        return self.hosts[host]

    def is_managed_worker(self, row):
        return row.get('mode') == 'terminal' and str(row.get('terminal_id', '')).startswith('ceohub-')

    @staticmethod
    def _reconnect_fingerprint(host, thread_id, terminal_id, origin, controller_id):
        return hashlib.sha256(json.dumps([host, thread_id, terminal_id, origin, controller_id]).encode()).hexdigest()

    def get_agent(self, host, thread_id, sessions=None):
        row = self.store.agent(host, thread_id)
        if not row:
            raise ValueError('not registered')
        records = self.terminals.records.values() if sessions is None else sessions
        runtime = next((r for r in records if r['id'] == row['terminal_id']), {})
        return {**row, 'connected': runtime.get('connected', False), 'human_busy': self.human_busy,
                'recovery': self.recoveries.get((host, thread_id), {})}

    def reconnect_agent(self, host, thread_id, *, origin, request_key):
        self.calls.append((host, thread_id, request_key))
        row = self.get_agent(host, thread_id)
        fingerprint = self._reconnect_fingerprint(host, thread_id, row['terminal_id'], origin, None)
        self.store.begin_operation('agent-reconnect:' + request_key, fingerprint)
        if self.callback:
            self.callback()
        result = self.answer or {'operation_state': 'done', 'recovery': {'code': 'reconnected'}}
        if callable(result):
            result = result(request_key)
        if isinstance(result, Exception):
            raise result
        recovery = {**result.get('recovery', {}), 'request_key': request_key, 'request_origin': origin}
        result = {**result, 'recovery': recovery}
        self.recoveries[(host, thread_id)] = recovery
        self.store.finish_operation('agent-reconnect:' + request_key,
                                    {'state': result['operation_state'], 'recovery': recovery})
        if result['operation_state'] == 'done':
            self.terminals.records[row['terminal_id']]['connected'] = True
        return result


class AutoReconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'hub.sqlite'
        self.hub = Hub(self.path)
        self.clock = Clock()
        self.scheduler = AutoReconnect(self.hub, clock=self.clock)

    def tearDown(self):
        self.scheduler.stop()
        self.hub.store.close()
        self.temp.cleanup()

    def configure(self, enabled=True, scheduler=None, key='config'):
        return (scheduler or self.scheduler).configure('remote', 'worker', enabled, TERMINAL, key)

    def saved(self):
        return self.hub.store.setting(self.scheduler._key('remote', 'worker'))

    def view(self):
        return self.scheduler.view(self.hub.get_agent('remote', 'worker'))

    def due(self):
        self.clock.now += INTERVAL_SECONDS

    def crash_claim(self, *, operation=None, connected=False, fingerprint=None):
        self.configure()
        value = self.saved()
        key = 'auto-reconnect-' + 'a' * 32
        value.update(status='reconnecting', request_key=key, uncertain_request_key=key,
                     last_attempt_at=self.clock.now, next_attempt_at=self.clock.now + 600)
        self.hub.store.setting(self.scheduler._key('remote', 'worker'), value)
        if operation is not None:
            actual = fingerprint or self.hub._reconnect_fingerprint('remote', 'worker', TERMINAL, 'ui', None)
            self.hub.store.begin_operation('agent-reconnect:' + key, actual)
            if operation != 'started':
                self.hub.store.finish_operation('agent-reconnect:' + key, operation)
        self.hub.terminals.records[TERMINAL]['connected'] = connected
        self.hub.recoveries[('remote', 'worker')] = {'code': 'reconnected', 'request_key': 'old-success'}
        return key

    def test_default_off_does_not_query_display_service(self):
        self.hub.terminals.request = lambda *a, **kw: self.fail('default off must not inspect broker')
        self.assertEqual(self.view(), AutoReconnect.disabled_view())
        self.scheduler.tick()
        self.assertEqual(self.hub.calls, [])

    def test_enabling_disconnected_waits_exactly_ten_minutes(self):
        value = self.configure()
        self.assertEqual(value['next_attempt_at'], 1600)
        self.scheduler.tick()
        self.clock.now = 1599.99
        self.scheduler.tick()
        self.assertFalse(self.hub.calls)
        self.clock.now = 1600
        self.scheduler.tick()
        self.assertEqual(len(self.hub.calls), 1)
        self.assertEqual(self.view()['status'], 'connected')

    def test_connected_has_no_timer_until_first_observed_disconnect(self):
        self.hub.terminals.records[TERMINAL]['connected'] = True
        self.assertIsNone(self.configure()['next_attempt_at'])
        self.scheduler.tick()
        self.clock.now = 100000
        self.hub.terminals.records[TERMINAL]['connected'] = False
        self.assertEqual(self.view()['status'], 'waiting')
        self.scheduler.tick()
        self.assertEqual(self.view()['next_attempt_at'], 100600)
        self.assertFalse(self.hub.calls)

    def test_restart_preserves_due_and_attempts_once_without_catchup(self):
        self.configure()
        self.clock.now = 20000
        restored = AutoReconnect(self.hub, clock=self.clock)
        restored.tick()
        restored.tick()
        self.assertEqual(len(self.hub.calls), 1)
        self.assertEqual(restored.view(self.hub.get_agent('remote', 'worker'))['status'], 'connected')

    def test_route_failure_retries_same_key_only_every_ten_minutes(self):
        self.hub.answer = {'operation_state': 'blocked', 'recovery': {
            'code': 'route_unavailable', 'retryable': True, 'message': ROUTE_MESSAGE, 'next_action': 'check_connection'}}
        self.configure(); self.due(); self.scheduler.tick()
        key = self.hub.calls[0][2]
        self.scheduler.tick()
        self.clock.now += 599
        self.scheduler.tick()
        self.assertEqual(len(self.hub.calls), 1)
        self.clock.now += 1
        self.scheduler.tick()
        self.assertEqual([call[2] for call in self.hub.calls], [key, key])

    def test_post_attachment_route_error_is_not_retryable(self):
        self.hub.answer = {'operation_state': 'blocked', 'recovery': {'code': 'route_unavailable'}}
        self.configure(); self.due(); self.scheduler.tick()
        self.due(); self.scheduler.tick()
        self.assertEqual(len(self.hub.calls), 1)
        self.assertEqual(self.view()['status'], 'needs_attention')
        self.assertTrue(self.saved()['uncertain_request_key'])

    def test_next_successful_connection_cycle_uses_new_request_key(self):
        self.configure(); self.due(); self.scheduler.tick()
        first = self.hub.calls[-1][2]
        self.hub.terminals.records[TERMINAL]['connected'] = False
        self.scheduler.tick()
        self.due(); self.scheduler.tick()
        self.assertEqual(len(self.hub.calls), 2)
        self.assertNotEqual(self.hub.calls[-1][2], first)

    def test_missing_original_auth_or_host_key_pause_without_new_sessions(self):
        for code in ('original_session_missing', 'ssh_auth_required', 'ssh_host_key_required', 'local_display_unavailable'):
            with self.subTest(code=code):
                self.hub.calls.clear()
                self.hub.recoveries.clear()
                self.hub.answer = {'operation_state': 'blocked', 'recovery': {'code': code}}
                self.configure(key=code); self.due(); self.scheduler.tick()
                self.due(); self.scheduler.tick()
                self.assertEqual(len(self.hub.calls), 1)
                self.assertEqual(self.view()['last_result']['code'], code)
                self.assertIsNone(self.view()['next_attempt_at'])

    def test_unknown_response_and_exception_keep_uncertain_key_after_toggle(self):
        for answer in ({'operation_state': 'unknown', 'recovery': {'code': 'reconnect_unknown'}}, RuntimeError('private-output')):
            with self.subTest(answer=type(answer).__name__):
                self.hub.calls.clear(); self.hub.recoveries.clear()
                # Use a new generation namespace, as a prior unknown must never
                # be escaped by changing the enable switch alone.
                self.hub.store.setting(self.scheduler._key('remote', 'worker'), {})
                self.hub.answer = answer
                self.configure(); self.due(); self.scheduler.tick()
                key = self.saved()['uncertain_request_key']
                self.configure(False, key='off'); self.configure(True, key='on')
                self.assertEqual(self.saved()['uncertain_request_key'], key)
                self.hub.terminals.records[TERMINAL]['connected'] = True
                self.hub.recoveries[('remote', 'worker')] = {'code': 'reconnected', 'request_key': 'unrelated-success'}
                self.scheduler.tick()
                self.hub.terminals.records[TERMINAL]['connected'] = False
                self.due(); self.scheduler.tick()
                self.assertEqual(len(self.hub.calls), 1)
                self.assertEqual(self.view()['status'], 'needs_attention')

    def test_crash_claim_absent_started_unknown_or_wrong_target_is_fenced(self):
        cases = ((None, None), ('started', None),
                 ({'state': 'unknown', 'recovery': {'code': 'reconnect_unknown'}}, None),
                 ({'state': 'done', 'recovery': {'code': 'reconnected'}}, 'wrong-fingerprint'))
        for operation, fingerprint in cases:
            with self.subTest(operation=operation, fingerprint=fingerprint):
                with self.hub.store.lock, self.hub.store.db:
                    self.hub.store.db.execute('delete from operations')
                self.hub.store.setting(self.scheduler._key('remote', 'worker'), {})
                self.hub.recoveries.clear()
                self.crash_claim(operation=operation, connected=True, fingerprint=fingerprint)
                self.scheduler.tick(); self.scheduler.tick()
                self.configure(False, key='off'); self.configure(True, key='on')
                self.due(); self.scheduler.tick()
                self.assertEqual(self.view()['status'], 'needs_attention')
                self.assertFalse(self.hub.calls)

    def test_matching_completed_receipt_resolves_crash_without_replay(self):
        self.crash_claim(operation={'state': 'done', 'recovery': {'code': 'reconnected'}}, connected=True)
        self.scheduler.tick()
        self.assertEqual(self.view()['status'], 'connected')
        self.assertIsNone(self.saved()['uncertain_request_key'])
        self.assertFalse(self.hub.calls)

    def test_unknown_legacy_claim_without_new_fence_field_still_pauses(self):
        self.crash_claim(connected=True)
        value = self.saved();value.pop('uncertain_request_key')
        self.hub.store.setting(self.scheduler._key('remote', 'worker'), value)
        self.scheduler.tick();self.scheduler.tick()
        self.assertEqual(self.view()['status'], 'needs_attention')
        self.assertTrue(self.saved()['uncertain_request_key'])

    def test_malformed_persisted_receipt_is_unknown_not_a_public_error(self):
        self.crash_claim(operation={'state': 'done', 'recovery': {'code': {'private': 'fixture'}}}, connected=True)
        self.scheduler.tick()
        self.assertEqual(self.view()['status'], 'needs_attention')
        self.assertNotIn('private', json.dumps(self.view()))
        self.assertFalse(self.hub.calls)

    def test_preexisting_manual_unknown_cannot_start_a_new_auto_request(self):
        self.hub.recoveries[('remote', 'worker')] = {'code': 'reconnect_unknown', 'request_key': 'manual'}
        self.assertEqual(self.configure()['status'], 'needs_attention')
        self.due();self.scheduler.tick()
        self.assertFalse(self.hub.calls)

    def test_unavailable_broker_does_not_mean_no_sessions_or_launch_an_owner(self):
        self.configure();self.due()
        self.hub.terminals.error = RuntimeError('fixture secret should not leave error boundary')
        self.scheduler.tick()
        self.assertFalse(self.hub.calls)
        self.assertEqual(self.view()['last_result']['code'], 'display_service_unavailable')
        self.assertNotIn('secret', json.dumps(self.view()))

    def test_closed_or_replaced_terminal_retires_old_opt_in(self):
        for mode, tid in (('closed', TERMINAL), ('close_unknown', TERMINAL), ('terminal', OTHER)):
            with self.subTest(mode=mode, tid=tid):
                self.hub.store.remember('remote', 'worker', 'Work', mode='terminal', terminal_id=TERMINAL)
                self.configure()
                self.hub.store.remember('remote', 'worker', 'Work', mode=mode, terminal_id=tid)
                self.due();self.scheduler.tick()
                self.assertEqual(self.view(), AutoReconnect.disabled_view())
                self.assertFalse(self.saved()['enabled'])
                self.assertFalse(self.hub.calls)

    def test_configuration_rejects_stale_generation_local_and_non_boolean(self):
        with self.assertRaises(ValueError):self.scheduler.configure('remote', 'worker', True, OTHER, 'stale')
        for value in (1, 0, 'true', None):
            with self.assertRaises(ValueError):self.configure(value)
        self.hub.hosts['remote']['transport'] = 'local'
        with self.assertRaises(ValueError):self.configure()

    def test_draft_delays_one_interval_without_clear_or_task_input(self):
        self.configure();self.due();self.hub.human_busy = True
        self.scheduler.tick()
        self.assertFalse(self.hub.calls)
        self.assertEqual(self.view()['last_result']['code'], 'human_input_pending')
        self.assertEqual(self.view()['next_attempt_at'], self.clock.now + 600)

    def test_disable_during_accepted_action_is_immediate_and_not_overwritten(self):
        self.configure();self.due()
        entered, release = threading.Event(), threading.Event()
        self.hub.callback = lambda: (entered.set(), release.wait(2))
        errors = []
        def run():
            try:self.scheduler.tick()
            except Exception as error:errors.append(error)
        thread = threading.Thread(target=run);thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertFalse(self.configure(False, key='off')['enabled'])
        finally:
            release.set();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertFalse(errors)
        self.assertFalse(self.saved()['enabled'])
        self.assertEqual(self.saved()['status'], 'off')

    def test_cancel_while_waiting_for_registry_prevents_action(self):
        self.configure();self.due()
        with self.hub.registry_lock:
            thread = threading.Thread(target=self.scheduler.tick);thread.start()
            # Observe the worker's completed local registry query rather than
            # requiring an arbitrary scheduling sleep.
            for _ in range(1000):
                if self.hub.terminals.queries:break
                threading.Event().wait(.001)
            self.scheduler.cancel('remote', 'worker', TERMINAL)
        thread.join(2)
        self.assertFalse(thread.is_alive());self.assertFalse(self.hub.calls)

    def test_stopping_during_action_leaves_crash_fence_for_next_owner(self):
        self.configure();self.due()
        self.hub.callback = self.scheduler.stop
        self.scheduler.tick()
        self.assertEqual(self.saved()['status'], 'reconnecting')
        self.assertTrue(self.saved()['uncertain_request_key'])
        self.assertEqual(len(self.hub.calls), 1)

    def test_two_tick_callers_never_overlap_one_accepted_action(self):
        self.configure();self.due()
        entered, release = threading.Event(), threading.Event()
        self.hub.callback = lambda: (entered.set(), release.wait(2))
        thread=threading.Thread(target=self.scheduler.tick);thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.scheduler.tick()
            self.assertEqual(len(self.hub.calls), 1)
        finally:release.set();thread.join(2)

    def test_settings_cas_prevents_two_independent_owners_claiming_same_due(self):
        self.configure();self.due()
        other_hub = Hub(self.path)
        other_hub.terminals = self.hub.terminals
        other = AutoReconnect(other_hub, clock=self.clock)
        barrier = threading.Barrier(2)
        errors = []
        for scheduler in (self.scheduler, other):
            original = scheduler._cas
            def synchronized(host, thread_id, before, after, original=original):
                if after.get('status') == 'reconnecting' and before.get('status') != 'reconnecting':
                    barrier.wait(timeout=2)
                return original(host, thread_id, before, after)
            scheduler._cas = synchronized
        def run(scheduler):
            try:scheduler.tick()
            except Exception as error:errors.append(error)
        threads = [threading.Thread(target=run, args=(scheduler,)) for scheduler in (self.scheduler, other)]
        try:
            for thread in threads:thread.start()
            for thread in threads:thread.join(3)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertFalse(errors)
            self.assertEqual(len(self.hub.calls) + len(other_hub.calls), 1)
        finally:
            other.stop();other_hub.store.close()


if __name__ == '__main__':
    unittest.main()
