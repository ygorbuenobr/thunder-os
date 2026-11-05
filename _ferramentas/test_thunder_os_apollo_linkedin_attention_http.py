"""Authenticated, read-only attention projection and failure visibility."""
from types import SimpleNamespace
from urllib.parse import urlsplit
import unittest
from unittest.mock import Mock
import painel_servidor as panel

class AttentionHttpTests(unittest.TestCase):
    def setUp(self):
        self.handler = object.__new__(panel.Handler)
        self.handler.server = SimpleNamespace(thunder_service=Mock(), thunder_apollo_linkedin_attention=SimpleNamespace(snapshot=Mock(return_value={
            'status': 'partial', 'items': [{'task_id': 'task', 'reason': 'Conta indisponível',
                'stopped_at': '2026-11-03T10:00:00Z', 'next_action': 'Verificar conta'}]})))
        self.handler.thunder_proxy_actor = Mock(return_value='ygor')
        self.handler.responder = lambda status, body: (status, body)
        self.handler.command = 'GET'

    def request(self, query=''):
        return self.handler.thunder_api(urlsplit('/api/os/v1/integrations/apollo/linkedin-attention' + query))

    def test_route_is_authenticated_read_only_and_preserves_partial_coverage(self):
        self.handler.thunder_proxy_actor.return_value = 'other'
        with self.assertRaises(panel.Erro): self.request()
        self.handler.thunder_proxy_actor.return_value = 'ygor'
        with self.assertRaises(panel.Erro): self.request('?other=1')
        self.handler.command = 'POST'
        with self.assertRaises(panel.Erro): self.request()
        self.handler.command = 'GET'
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body['status'], 'partial')
        self.assertEqual(body['items'][0]['next_action'], 'Verificar conta')
        self.handler.server.thunder_apollo_linkedin_attention.snapshot.assert_called_once()

    def test_route_projects_reader_through_executor(self):
        from thunder_os.apollo_linkedin_attention import ApolloLinkedInAttention
        task = {'id': 'task', 'type': 'linkedin_step_connect', 'status': 'scheduled',
                'emailer_campaign_id': 'campaign', 'user_id': 'owner', 'created_at': '2026-11-03T10:00:00Z'}
        reader = Mock()
        reader.list_open_tasks.return_value = {'tasks': [task], 'coverage': 'unknown'}
        reader.get.return_value = task
        executor = Mock()
        executor.prepare.return_value = {'state': 'blocked', 'reason': 'default_account_unavailable'}
        service = SimpleNamespace(list_apollo_linkedin_reservations=lambda: [],
                                  list_apollo_linkedin_account_locks=lambda: [])
        self.handler.server.thunder_apollo_linkedin_attention = ApolloLinkedInAttention(
            service, reader, executor, company_id='company', team_id='team')
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body['coverage'], 'unknown')
        self.assertEqual(body['items'][0]['reason'], 'default_account_unavailable')
        self.assertEqual(body['items'][0]['stopped_at'], task['created_at'])
        executor.prepare.assert_called_once()

    def test_search_result_deleted_before_detail_is_visible_and_not_prepared(self):
        from thunder_os.apollo_linkedin_attention import ApolloLinkedInAttention
        from thunder_os.apollo_linkedin_reader import ApolloLinkedInReader
        import json
        task = {'id': 'gone', 'type': 'linkedin_step_connect', 'status': 'scheduled', 'user_id': 'owner'}
        for code in (404, 422):
            def request(method, path, body, headers, timeout):
                if path.endswith('api_profile'):
                    return 200, json.dumps({'user': {'id': 'actor', 'team_id': 'team'}}).encode()
                if path.endswith('/search'):
                    return 200, json.dumps({'tasks': [task], 'pagination': {
                        'page': 1, 'per_page': 100, 'total_entries': 1, 'total_pages': 1}}).encode()
                return code, b'{}'
            reader = ApolloLinkedInReader(lambda: 'fake', team_id='team', api_actor_id='actor',
                                          owner_ids=['owner'], request=request)
            executor = Mock()
            service = SimpleNamespace(list_apollo_linkedin_reservations=lambda: [],
                                      list_apollo_linkedin_account_locks=lambda: [])
            result = ApolloLinkedInAttention(service, reader, executor, company_id='c', team_id='team').snapshot()
            with self.subTest(status=code):
                self.assertEqual(result['items'][0]['reason'], 'task_removed')
                self.assertFalse(result['items'][0]['execution_enabled'])
                self.assertIn('nem marcar como concluída', result['items'][0]['next_action'])
                executor.prepare.assert_not_called()

    def test_get_does_not_write_locks_or_reservations(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from thunder_os.db import Repository
        from thunder_os.service import ThunderService
        from thunder_os.apollo_linkedin_attention import ApolloLinkedInAttention
        from thunder_os.apollo_linkedin_executor import DryRunExecutor
        from thunder_os.apollo_linkedin_reader import CampaignBindings
        with tempfile.TemporaryDirectory() as tmp, patch('thunder_os.db.OFFICIAL_PANEL_DB', Path(tmp) / 'panel.db'):
            (Path(tmp) / 'panel.db').touch()
            service = ThunderService(Repository(str(Path(tmp) / 'panel.db')))
            credential = lambda: 'token'
            credential.credential_ref = 'ref'
            client = SimpleNamespace(account_id='account', expected_public_id='sender',
                                     request=SimpleNamespace(account_id='account', credential=credential))
            bindings = CampaignBindings([dict(company_id='company', team_id='team', campaign_id='campaign',
                account_id='account', public_id='sender', credential_ref='ref', owner_ids=['owner'])])
            task = dict(id='task', type='linkedin_step_connect', status='scheduled', emailer_campaign_id='campaign',
                        user_id='owner', contact={'linkedin_url': 'https://linkedin.com/in/person/'})
            reader = Mock(); reader.list_open_tasks.return_value = {'tasks': [task], 'coverage': 'complete'}
            reader.get.return_value = task
            executor = DryRunExecutor(service, reader, bindings, lambda _: {'id': 'account', 'available': True}, client=client)
            self.handler.server.thunder_apollo_linkedin_attention = ApolloLinkedInAttention(
                service, reader, executor, company_id='company', team_id='team')
            before = (service.list_apollo_linkedin_account_locks(), service.list_apollo_linkedin_reservations())
            statements = []
            connect = service.repository._connect
            def traced_connect():
                connection = connect()
                connection.set_trace_callback(statements.append)
                return connection
            with patch.object(service.repository, '_connect', side_effect=traced_connect), \
                 patch.object(service, 'apollo_linkedin_account_lock', side_effect=AssertionError('lock write')), \
                 patch.object(service, 'reserve_apollo_linkedin', side_effect=AssertionError('reservation write')):
                status, body = self.request()
            self.assertFalse([sql for sql in statements if sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE'))])
            self.assertEqual((status, body['status']), (200, 'ok'))
            self.assertEqual(before, (service.list_apollo_linkedin_account_locks(), service.list_apollo_linkedin_reservations()))

    def test_missing_timestamp_uses_observed_age(self):
        from thunder_os.apollo_linkedin_attention import ApolloLinkedInAttention
        task = {'id': 'task', 'type': 'linkedin_step_connect', 'status': 'scheduled',
                'emailer_campaign_id': 'campaign', 'user_id': 'owner'}
        reader = Mock(); reader.list_open_tasks.return_value = {'tasks': [task], 'coverage': 'unknown'}
        reader.get.return_value = task
        executor = Mock(); executor.prepare.return_value = {'state': 'blocked', 'reason': 'blocked'}
        service = SimpleNamespace(list_apollo_linkedin_reservations=lambda: [], list_apollo_linkedin_account_locks=lambda: [])
        attention = ApolloLinkedInAttention(service, reader, executor, company_id='company', team_id='team')
        first = attention.snapshot()['items'][0]; second = attention.snapshot()['items'][0]
        self.assertEqual(first['age_basis'], 'first_observed')
        self.assertTrue(first['stopped_at'])
        self.assertEqual(first['stopped_at'], second['stopped_at'])

    def test_missing_adapter_is_unavailable_not_empty_success(self):
        self.handler.server.thunder_apollo_linkedin_attention = None
        self.handler.server.thunder_service = SimpleNamespace()
        self.assertEqual(self.request(), (200, {'status': 'unavailable', 'items': [], 'reason': 'configuration_missing'}))

    def test_uncertain_reservations_are_visible_with_incomplete_coverage(self):
        self.handler.server.thunder_apollo_linkedin_attention = None
        self.handler.server.thunder_service = SimpleNamespace(list_apollo_linkedin_reservations=lambda: [
            {'task_id': 'uncertain', 'state': 'uncertain', 'reason': 'timeout', 'updated_at': '2026-11-03T10:00:00Z'},
            {'task_id': 'confirmed', 'state': 'confirmed'}])
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body['status'], 'partial')
        self.assertEqual([item['task_id'] for item in body['items']], ['uncertain'])
        self.assertEqual(body['items'][0]['reason'], 'timeout')
        self.assertIn('Verificar', body['items'][0]['next_action'])

    def test_orphan_reservation_visible_in_fallback(self):
        self.handler.server.thunder_apollo_linkedin_attention = None
        self.handler.server.thunder_service = SimpleNamespace(list_apollo_linkedin_reservations=lambda: [
            {'task_id': 'orphan', 'state': 'reserved'}])
        self.assertEqual(self.request()[1]['items'][0]['task_id'], 'orphan')

    def test_protected_fixture_is_visible_and_never_prepared(self):
        from thunder_os.apollo_linkedin_attention import ApolloLinkedInAttention, EXCLUDED_TASK
        reader = Mock(); reader.list_open_tasks.return_value = {'tasks': [{'id': EXCLUDED_TASK}], 'coverage': 'complete'}
        executor = Mock()
        service = SimpleNamespace(list_apollo_linkedin_reservations=lambda: [], list_apollo_linkedin_account_locks=lambda: [])
        result = ApolloLinkedInAttention(service, reader, executor, company_id='c', team_id='t').snapshot()
        self.assertEqual(result['items'][0]['reason'], 'fixture_protegida')
        self.assertFalse(result['items'][0]['execution_enabled'])
        reader.get.assert_not_called(); executor.prepare.assert_not_called()

    def test_protected_fixture_blocked_even_if_passed_directly_to_executor(self):
        from thunder_os.apollo_linkedin_attention import EXCLUDED_TASK
        from thunder_os.apollo_linkedin_executor import DryRunExecutor
        executor = object.__new__(DryRunExecutor)
        self.assertEqual(executor.prepare({'id': EXCLUDED_TASK}, company_id='c', team_id='t'),
                         {'state': 'blocked', 'reason': 'fixture_protegida'})

    def test_malformed_bindings_are_visible_without_crashing(self):
        server = SimpleNamespace(thunder_service=SimpleNamespace(list_apollo_linkedin_reservations=lambda: []))
        env = {'THUNDER_APOLLO_LI_BINDINGS': '[null]', 'THUNDER_APOLLO_LI_COMPANY': 'c',
               'THUNDER_APOLLO_LI_TEAM': 't', 'THUNDER_APOLLO_LI_API_ACTOR': 'a'}
        self.assertIsNone(panel.configure_apollo_linkedin_attention(server, lambda: 'fake', env))
        self.handler.server = server
        self.assertEqual(self.request()[1]['reason'], 'configuration_invalid')

    def test_attention_and_preview_never_access_send_client(self):
        class ForbiddenProvider:
            @property
            def direct_client(self):
                raise AssertionError('GET accessed sender')
        server = SimpleNamespace(thunder_connectsafely_app=SimpleNamespace(provider=ForbiddenProvider()),
                                 thunder_apollo_linkedin_read_identity=SimpleNamespace(account_id='a', public_id='p'),
                                 thunder_service=SimpleNamespace(apollo_linkedin_store_ready=lambda: True))
        env = {'THUNDER_APOLLO_LI_BINDINGS': '[{"company_id":"c","team_id":"t","campaign_id":"s",'
               '"account_id":"a","public_id":"p","credential_ref":"r","owner_ids":["o"]}]',
               'THUNDER_APOLLO_LI_COMPANY':'c','THUNDER_APOLLO_LI_TEAM':'t','THUNDER_APOLLO_LI_API_ACTOR':'a',
               'THUNDER_APOLLO_LI_OWNER':'o','THUNDER_APOLLO_LI_SEQUENCE':'s','THUNDER_APOLLO_LI_TASK':'task',
               'THUNDER_APOLLO_LI_ACCOUNT':'a','THUNDER_APOLLO_LI_PUBLIC_ID':'p'}
        attention = panel.configure_apollo_linkedin_attention(server, lambda: 'fake', env)
        preview = panel.configure_apollo_linkedin_preview(server, lambda: 'fake', env)
        self.assertIsNotNone(attention)
        self.assertIsNotNone(preview)
        self.assertIsNone(attention.executor.client)
        self.assertIsNone(attention.executor.account_status_reader('a'))
        self.assertIsNone(preview._status())

    def test_attention_observes_account_but_does_not_authorize_execution(self):
        from thunder_os.apollo_linkedin_attention import ApolloLinkedInAttention
        from thunder_os.apollo_linkedin_executor import DryRunExecutor
        from thunder_os.apollo_linkedin_reader import CampaignBindings
        task = {'id': 'test-task', 'type': 'linkedin_step_connect', 'status': 'scheduled',
                'emailer_campaign_id': 'campaign', 'user_id': 'owner'}
        reader = Mock(); reader.list_open_tasks.return_value = {'tasks': [task], 'coverage': 'complete'}
        reader.get.return_value = task
        service = SimpleNamespace(apollo_linkedin_store_ready=lambda: True,
                                  list_apollo_linkedin_reservations=lambda: [],
                                  list_apollo_linkedin_account_locks=lambda: [])
        routes = CampaignBindings([dict(company_id='company', team_id='team', campaign_id='campaign',
            account_id='account', public_id='sender', credential_ref='ref', owner_ids=['owner'])])
        account_status = Mock(return_value={'id':'account', 'publicId':'sender', 'available':True,
                                           'source':'connectsafely', 'observed_at':'2026-11-04T00:00:00Z',
                                           'write_authorized':False})
        executor = DryRunExecutor(service, reader, routes, account_status, client=None)
        self.handler.server.thunder_apollo_linkedin_attention = ApolloLinkedInAttention(
            service, reader, executor, company_id='company', team_id='team')
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body['items'][0]['reason'], 'execution_binding_unverified')
        self.assertEqual(body['items'][0]['account_observation']['account_id'], 'account')
        account_status.assert_called_once_with('account')

    def test_deadline_returns_partial_even_if_reader_hangs(self):
        import threading
        from thunder_os.apollo_linkedin_attention import ApolloLinkedInAttention
        gate = threading.Event()
        reader = Mock(); reader.list_open_tasks.side_effect = lambda: gate.wait(2)
        service = SimpleNamespace(list_apollo_linkedin_reservations=lambda: [{'task_id': 'orphan', 'state': 'reserved'}], list_apollo_linkedin_account_locks=lambda: [])
        attention = ApolloLinkedInAttention(service, reader, Mock(), company_id='c', team_id='t', budget_seconds=.02)
        try:
            result = attention.snapshot()
            self.assertEqual(result['reason'], 'time_budget_exhausted')
            self.assertEqual(result['items'][0]['task_id'], 'orphan')
            self.assertEqual(attention.snapshot()['reason'], 'read_in_progress')
        finally:
            gate.set()

if __name__ == '__main__': unittest.main()
