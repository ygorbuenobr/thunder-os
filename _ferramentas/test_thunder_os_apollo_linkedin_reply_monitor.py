import io
import json
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from thunder_os.apollo_linkedin_identity_binding import KIND, binding_key
from thunder_os.apollo_linkedin_reply_coverage import ApolloContactReplyReader
from thunder_os.apollo_linkedin_reply_monitor import ApolloReplyMonitor, ApolloReplyMonitorWorker, main
from thunder_os.models import payload_hash


class Repo:
    def __init__(self):
        approved = {'company_id': 'tenant', 'team_id': 'team', 'sequence_id': 'sequence',
                    'account_id': 'account'}
        scope = {'context': {}, 'configuration': {'apollo_linkedin': approved}, 'campaign_config': {}}
        digest = payload_hash(scope)
        ident = binding_key('tenant', 'campaign', 'task', 'account', 'approval')
        self.binding = {'id': ident, 'version': 1, 'company': 'tenant', 'campaign_id': 'campaign',
            'approval_id': 'approval', 'campaign_version': 'v1', 'scope_hash': digest, 'team_id': 'team',
            'sequence_id': 'sequence', 'task_id': 'task', 'action_type': 'linkedin_step_message',
            'contact_id': 'contact', 'account_id': 'account', 'recipient_public_id': 'person',
            'member_urn': 'urn:li:fsd_profile:synthetic'}
        self.states = {(KIND, ident): self.binding,
            ('approval', 'approval'): {'version': 1, 'actor': 'ygor', 'scope': 'campaign_version', 'campaign_id': 'campaign',
                'campaign_version': 'v1', 'scope_hash': digest, 'snapshot_id': 'apollo-campaign-' + digest},
            ('campaign_version_snapshot', 'apollo-campaign-' + digest): {'version': 1, 'scope_hash': digest, 'scope': scope}}
        self.row = {'account_id': 'account', 'recipient': 'person', 'campaign_version': 'v1'}
        self.events_rows = []
        self.event = {'type': 'apollo_linkedin_campaign_reservation', 'payload': {
            'binding_id': ident, 'company': 'tenant', 'campaign_id': 'campaign', 'approval_id': 'approval',
            'approval_scope_hash': digest, 'apollo_team_id': 'team', 'apollo_sequence_id': 'sequence',
            'task_id': 'task', 'action_type': 'linkedin_step_message', 'campaign_version': 'v1',
            'apollo_contact_id': 'contact', 'account_id': 'account', 'member_urn': 'urn:li:fsd_profile:synthetic'}}
    def transaction(self): return nullcontext()
    def _connect(self):
        owner = self
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def set_progress_handler(self, *args): pass
            def execute(self, query, args):
                self.query, self.args = query, args
                return self
            def fetchone(self):
                if self.query.startswith('SELECT 1 FROM thunder_state'):
                    company = self.args[0]
                    return next((1 for (kind, _), value in owner.states.items()
                                 if kind == 'operator_alert' and value.get('company_id') == company
                                 and value.get('reason') == 'apollo_reply_coverage_unknown'
                                 and value.get('active') is True), None)
                kind, previous, company, team = self.args
                matches = sorted((ident, value) for (key, ident), value in owner.states.items()
                    if key == kind and ident > previous and value.get('company') == company
                    and value.get('team_id') == team)
                if not matches: return None
                ident, value = matches[0]
                return {'entity_id': ident, 'version': value.get('version', 1),
                        'payload': json.dumps(value), 'updated_at': 'now'}
        return Connection()
    def get_state(self, kind, ident): return self.states.get((kind, ident))
    def list_state(self, kind): return [v for (k, _), v in self.states.items() if k == kind]
    def get_apollo_linkedin_reservation(self, *args): return self.row
    def events(self, entity): return [self.event]
    def put_state(self, kind, ident, value): self.states[(kind, ident)] = value
    def append_event(self, ident, kind, value): self.events_rows.append((ident, kind, value))
    def put_states_atomic(self, items):
        kind, ident, value, version = items[0]
        self.states[(kind, ident)] = {**value, 'version': version + 1}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.events, self.fail_read = Repo(), [], False
        def request(method, path, body, headers, timeout):
            if self.fail_read: raise OSError('offline')
            if path.endswith('api_profile'):
                data = {'user': {'id': 'actor', 'team_id': 'team'}}
            elif path.endswith('/contact'):
                data = {'contact': {'id': 'contact', 'email_status': 'verified',
                    'contact_campaign_statuses': [{'emailer_campaign_id': 'sequence', 'status': 'active'}]}}
            else:
                data = {'contact_id': 'contact', 'events': self.events}
            return 200, json.dumps(data).encode()
        self.reader = ApolloContactReplyReader(lambda: 'fake', team_id='team', api_actor_id='actor', request=request)
        self.monitor = ApolloReplyMonitor(self.repo, self.reader, company='tenant', team='team')

    def test_reply_creates_one_conservative_stop_and_alert(self):
        self.events = [{'type': 'replied'}]
        self.assertEqual(self.monitor.run_once()['status'], 'reply')
        self.assertEqual(self.monitor.run_once()['status'], 'reply')
        observations = self.repo.list_state('apollo_reply_observation')
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]['classification'], 'unknown')
        self.assertEqual(len(self.repo.list_state('apollo_linkedin_reply_stop')), 1)
        self.assertEqual(len(self.repo.list_state('operator_alert')), 1)
        self.assertEqual(len(self.repo.events_rows), 1)

    def test_unavailable_source_is_unknown_and_does_not_invent_reply(self):
        self.fail_read = True
        result = self.monitor.run_once()
        self.assertEqual(result['coverage'], 'partial')
        self.assertEqual(result['contact_coverage'], 'unknown')
        self.assertEqual(self.repo.list_state('apollo_reply_observation'), [])

    def test_binding_or_event_mismatch_never_reads_or_stops(self):
        self.events = [{'type': 'replied'}]
        self.repo.event['payload']['account_id'] = 'other'
        self.assertEqual(self.monitor.run_once()['status'], 'unknown')
        self.assertEqual(self.repo.list_state('apollo_linkedin_reply_stop'), [])

    def test_other_tenant_is_not_observed(self):
        self.repo.binding['company'] = 'other'
        self.assertEqual(self.monitor.run_once()['status'], 'empty')

    def test_default_cli_never_constructs_repository(self):
        with patch.dict('os.environ', {}, clear=True), patch('sys.stdout', new_callable=io.StringIO):
            with patch('thunder_os.db.Repository', side_effect=AssertionError('unexpected')):
                self.assertEqual(main(['--company', 'tenant', '--team', 'team', '--actor', 'actor']), 2)

    def test_real_sqlite_persists_reply_stop_and_idempotence(self):
        from test_thunder_os_apollo_linkedin_campaign_reservation import CampaignReservationTest
        fixture = CampaignReservationTest('test_approved_campaign_reserves_once_and_records_audit')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.reserve()
        def request(method, path, body, headers, timeout):
            if path.endswith('api_profile'):
                value = {'user': {'id': 'actor', 'team_id': 'team'}}
            elif path.endswith('/contact1'):
                value = {'contact': {'id': 'contact1', 'email_status': 'verified',
                    'contact_campaign_statuses': [{'emailer_campaign_id': 'seq', 'status': 'active'}]}}
            else:
                value = {'contact_id': 'contact1', 'events': [{'type': 'replied'}]}
            return 200, json.dumps(value).encode()
        reader = ApolloContactReplyReader(lambda: 'fake', team_id='team', api_actor_id='actor', request=request)
        monitor = ApolloReplyMonitor(fixture.repo, reader, company='company', team='team')
        self.assertEqual(monitor.run_once()['status'], 'reply')
        self.assertEqual(monitor.run_once()['status'], 'reply')
        self.assertEqual(len(fixture.repo.list_state('apollo_reply_observation')), 1)
        self.assertEqual(len(fixture.repo.list_state('apollo_linkedin_reply_stop')), 1)
        self.assertEqual(len(fixture.repo.list_state('operator_alert')), 1)

    def test_scans_a_single_page_with_sqlite_limit_and_rotates(self):
        from test_thunder_os_apollo_linkedin_campaign_reservation import CampaignReservationTest
        fixture = CampaignReservationTest('test_approved_campaign_reserves_once_and_records_audit')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.reserve()
        # Rows from unrelated tenants never enter the monitor's selected page.
        for i in range(30):
            fixture.repo.append_state(KIND, 'other%03d' % i,
                                      {'company': 'different', 'team_id': 'team'})
        statements, original = [], fixture.repo._connect
        def traced():
            con = original()
            con.set_trace_callback(statements.append)
            return con
        reader = ApolloContactReplyReader(lambda: 'fake', team_id='team', api_actor_id='actor',
                                          request=lambda *args: (_ for _ in ()).throw(OSError('offline')))
        with patch.object(fixture.repo, '_connect', side_effect=traced):
            result = ApolloReplyMonitor(fixture.repo, reader, company='company', team='team').run_once()
        self.assertEqual(result['coverage'], 'partial')
        self.assertEqual(result['contact_coverage'], 'unknown')
        self.assertEqual(result['observed'], 1)
        selects = [sql for sql in statements if 'json_extract' in sql and 'SELECT' in sql]
        self.assertEqual(len(selects), 1)
        self.assertIn('LIMIT 1', selects[0])

    def test_worker_is_off_by_default_and_rate_limited(self):
        times = [100.0]
        calls = []
        class Monitor:
            repo = self.repo
            company = 'tenant'
            team = 'team'
            def run_once(self):
                calls.append(True)
                return {'status': 'reply', 'coverage': 'partial', 'binding_id': 'binding'}
        worker = ApolloReplyMonitorWorker(Monitor(), clock=lambda: times[0])
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(worker.process_one())
            self.assertEqual(calls, [])
        with patch.dict('os.environ', {'THUNDER_APOLLO_LI_REPLY_MONITOR_ENABLED': 'true'}, clear=True):
            self.assertTrue(worker.process_one())
            self.assertFalse(worker.process_one())
            times[0] += 60
            self.assertTrue(worker.process_one())
        self.assertEqual(len(calls), 2)

    def test_disabled_runtime_can_monitor_without_enabling_outreach(self):
        from thunder_os.worker import DisabledRuntime
        calls = []
        class Monitor:
            def process_one(self): calls.append('monitor'); return True
        runtime = DisabledRuntime(apollo_reply_monitor=Monitor())
        runtime._health = lambda *_: None
        self.assertFalse(runtime.process_one())
        self.assertEqual(calls, ['monitor'])

    def test_worker_build_default_never_constructs_monitor(self):
        from thunder_os import worker as worker_module
        with patch.dict('os.environ', {}, clear=True):
            with patch.object(worker_module, '_build_apollo_reply_monitor', side_effect=AssertionError('unexpected')):
                runtime = worker_module.build_runtime()
        self.assertIsNone(runtime.apollo_reply_monitor)

    def test_worker_build_uses_official_store_without_enabling_outreach(self):
        import tempfile
        from pathlib import Path
        from thunder_os import worker as worker_module
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'official.db'
            db.touch()
            monitor = object()
            env = {'PAINEL_DB': str(db), 'THUNDER_APOLLO_LI_OFFICIAL_DB': str(db),
                   'THUNDER_APOLLO_LI_REPLY_MONITOR_ENABLED': 'true'}
            with patch.dict('os.environ', env, clear=True):
                with patch.object(worker_module, '_build_apollo_reply_monitor', return_value=monitor) as build:
                    runtime = worker_module.build_runtime()
            self.assertIsInstance(runtime, worker_module.DisabledRuntime)
            self.assertIs(runtime.apollo_reply_monitor, monitor)
            self.assertIsNone(runtime.reply_pause_worker)
            self.assertEqual(build.call_count, 1)

    def test_worker_persists_unknown_source_and_other_success_does_not_hide_it(self):
        repo = self.repo
        class Monitor:
            company = 'tenant'
            team = 'team'
            def __init__(self): self.repo = repo; self.results = [
                {'status': 'unknown', 'coverage': 'partial', 'contact_coverage': 'unknown', 'binding_id': 'a'},
                {'status': 'ok', 'coverage': 'partial', 'contact_coverage': 'complete', 'binding_id': 'b'},
                {'status': 'ok', 'coverage': 'partial', 'contact_coverage': 'complete', 'binding_id': 'a'}]
            def run_once(self): return self.results.pop(0)
        clock = [100.0]
        worker = ApolloReplyMonitorWorker(Monitor(), clock=lambda: clock[0])
        with patch.dict('os.environ', {'THUNDER_APOLLO_LI_REPLY_MONITOR_ENABLED': 'true'}, clear=True):
            self.assertFalse(worker.process_one())
            self.assertTrue(worker.degraded)
            status = repo.list_state('apollo_reply_monitor_status')[0]
            self.assertEqual((status['source'], status['status']), ('apollo', 'unknown'))
            self.assertIn('observed_at', status)
            self.assertTrue(repo.list_state('operator_alert')[0]['active'])
            clock[0] += 60
            self.assertFalse(worker.process_one())
            self.assertTrue(worker.degraded)
            clock[0] += 60
            self.assertFalse(worker.process_one())
            self.assertFalse(worker.degraded)
            self.assertFalse(repo.list_state('operator_alert')[0]['active'])

    def test_disabled_health_reports_degraded_monitor_without_outreach(self):
        import json
        import tempfile
        from pathlib import Path
        from thunder_os.worker import DisabledRuntime
        class Monitor:
            degraded = True
        runtime = DisabledRuntime(apollo_reply_monitor=Monitor())
        with tempfile.TemporaryDirectory() as directory:
            runtime.health_path = Path(directory) / 'health.json'
            runtime._health()
            health = json.loads(runtime.health_path.read_text())
        self.assertEqual(health['status'], 'degraded')
        self.assertFalse(health['external_effects_enabled'])

    def test_stored_event_reconciliation_off_by_default_and_bounded(self):
        from thunder_os.worker import _StoredEventWorker
        cursor = 'a' * 64
        class Reconciler:
            def __init__(self, repo, *, account_id): self.calls = []
            def reconcile_pending(self, service, *, after, limit):
                self.calls.append((after, limit))
                return {'status': 'partial' if after is None else 'complete',
                        'next_cursor': cursor if after is None else None, 'items': []}
        times = [0.0]
        with patch('thunder_os.connectsafely_webhook.ConnectSafelyStoredEventReconciler', Reconciler):
            worker = _StoredEventWorker(self.repo, 'synthetic-account', clock=lambda: times[0])
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(worker.process_one())
            self.assertEqual(worker.reconciler.calls, [])
        with patch.dict('os.environ', {'THUNDER_OS_CONNECTSAFELY_EVENT_RECONCILE_ENABLED': 'true'}, clear=True):
            self.assertFalse(worker.process_one())
            self.assertFalse(worker.process_one())
            times[0] += 60
            self.assertFalse(worker.process_one())
        self.assertEqual(worker.reconciler.calls, [(None, 50), (cursor, 50)])

    def test_stored_event_budget_issue_is_visible_until_a_clean_scan(self):
        from thunder_os.worker import _StoredEventWorker
        cursor = 'a' * 64
        class Reconciler:
            def __init__(self, repo, *, account_id):
                self.results = [
                    {'status': 'partial', 'reason': 'binding_budget_exceeded',
                     'next_cursor': cursor, 'items': [{'status': 'unverified'}]},
                    {'status': 'complete', 'next_cursor': None, 'items': []},
                    {'status': 'complete', 'next_cursor': None, 'items': []}]
            def reconcile_pending(self, service, *, after, limit): return self.results.pop(0)
        times = [0.0]
        with patch('thunder_os.connectsafely_webhook.ConnectSafelyStoredEventReconciler', Reconciler):
            worker = _StoredEventWorker(self.repo, 'synthetic-account', clock=lambda: times[0])
        with patch.dict('os.environ', {'THUNDER_OS_CONNECTSAFELY_EVENT_RECONCILE_ENABLED': 'true'}, clear=True):
            worker.process_one()
            self.assertTrue(worker.degraded)
            self.assertEqual(self.repo.list_state('connectsafely_event_reconciliation_status')[0]['coverage'], 'partial')
            self.assertTrue(self.repo.list_state('operator_alert')[0]['active'])
            times[0] += 60
            worker.process_one()
            self.assertTrue(worker.degraded)
            times[0] += 60
            worker.process_one()
            self.assertFalse(worker.degraded)
            self.assertFalse(self.repo.list_state('operator_alert')[0]['active'])

    def test_one_contact_does_not_claim_population_coverage(self):
        first = self.repo.binding
        second = {**first, 'id': 'z' + first['id']}
        self.repo.states[(KIND, second['id'])] = second
        self.events = [{'type': 'replied'}]
        one = self.monitor.run_once()
        self.assertEqual(one['status'], 'reply')
        self.assertEqual(one['coverage'], 'partial')
        self.assertEqual(one['contact_coverage'], 'complete')
        self.assertEqual(one['observed'], 1)


if __name__ == '__main__':
    unittest.main()
