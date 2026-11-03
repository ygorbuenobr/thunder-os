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

if __name__ == '__main__': unittest.main()
