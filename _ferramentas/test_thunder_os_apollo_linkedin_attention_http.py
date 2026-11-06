"""Authenticated, read-only attention projection and failure visibility."""
from types import SimpleNamespace
from urllib.parse import urlsplit
import unittest
from unittest.mock import Mock, patch
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

    def test_attention_marks_accepted_in_process_read(self):
        from thunder_os.apollo_linkedin_attention import ApolloLinkedInAttention
        reader = Mock()
        reader.list_open_tasks.return_value = {'tasks': [], 'coverage': 'complete'}
        service = SimpleNamespace(list_apollo_linkedin_reservations=lambda: [],
                                  list_apollo_linkedin_account_locks=lambda: [])
        self.handler.server.thunder_apollo_linkedin_attention = ApolloLinkedInAttention(
            service, reader, Mock(), company_id='company', team_id='team',
            read_isolation='in_process_accepted')
        _, body = self.request()
        self.assertEqual('in_process_accepted', body['read_isolation'])

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
        self.assertEqual(self.request(), (200, {'status': 'unavailable', 'items': [], 'reason': 'configuration_missing', 'read_isolation': None}))

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

    def test_attention_keeps_reconcile_pending_and_scopes_company(self):
        from thunder_os.apollo_linkedin_attention import ApolloLinkedInAttention
        rows = [
            {'company': 'c', 'task_id': 'p', 'state': 'confirmed', 'apollo_status': 'pending'},
            {'company': 'c', 'task_id': 'crash', 'state': 'confirmed', 'apollo_status': 'attempted'},
            {'company': 'c', 'task_id': 'u', 'state': 'uncertain'},
            {'company': 'c', 'task_id': 'r', 'state': 'reserved'},
            {'company': 'other', 'task_id': 'secret', 'state': 'uncertain'},
            {'company': 'c', 'task_id': 'done', 'state': 'confirmed', 'apollo_status': 'completed'},
        ]
        service = SimpleNamespace(list_apollo_linkedin_reservations=lambda: rows,
                                  list_apollo_linkedin_account_locks=lambda: [])
        reader = Mock(); reader.list_open_tasks.return_value = {'tasks': [], 'coverage': 'complete'}
        result = ApolloLinkedInAttention(service, reader, Mock(), company_id='c', team_id='t').snapshot()
        self.assertEqual([r['task_id'] for r in result['items']], ['p', 'crash', 'u', 'r'])
        self.assertEqual(result['items'][0]['reason'], 'apollo_reconcile_pending')
        self.assertIn('não reenviar', result['items'][0]['next_action'])
        self.assertEqual(result['items'][1]['reason'], 'apollo_reconcile_pending')
        self.assertIn('antes de qualquer nova conclusão', result['items'][1]['next_action'])
        self.assertIn('não reenviar', result['items'][1]['next_action'])
        self.assertTrue(all(item['execution_enabled'] is False for item in result['items']))

    def test_reconcile_pending_visible_in_fallback(self):
        self.handler.server.thunder_apollo_linkedin_attention = None
        self.handler.server.thunder_service = SimpleNamespace(list_apollo_linkedin_reservations=lambda: [
            {'task_id': 'p', 'state': 'confirmed', 'apollo_status': 'pending'},
            {'task_id': 'crash', 'state': 'confirmed', 'apollo_status': 'attempted'}])
        items = self.request()[1]['items']
        self.assertEqual([item['reason'] for item in items], ['apollo_reconcile_pending'] * 2)
        self.assertTrue(all('não reenviar' in item['next_action'] for item in items))
        self.assertIn('antes de qualquer nova conclusão', items[1]['next_action'])

    def test_uncertain_resolution_fails_closed_without_verified_roles(self):
        from thunder_os.apollo_linkedin_roles import RolesUnavailable
        self.handler.command = 'POST'
        path = urlsplit('/api/os/v1/integrations/apollo/linkedin-uncertain/resolve')
        # A missing role file is unavailable for every authenticated actor;
        # operator denial is separately exercised with a valid roles file.
        with patch('thunder_os.apollo_linkedin_roles.authorize_manager', side_effect=RolesUnavailable('missing')):
            for actor in ('ygor', 'other'):
                self.handler.thunder_proxy_actor.return_value = actor
                with self.assertRaises(panel.Erro) as exc:
                    self.handler.thunder_api(path)
                self.assertEqual(exc.exception.status, 503)
        self.assertEqual(self.handler.server.thunder_service.mock_calls, [])

    def test_canary_uses_approved_campaign_and_provider_identity(self):
        from contextlib import nullcontext
        self.handler.command = 'POST'
        self.handler.thunder_actor = Mock(return_value='ygor')
        self.handler.corpo = Mock(return_value={'company': 'company', 'task_id': 'task',
            'campaign_id': 'campaign', 'approval_id': 'approval', 'campaign_version': 'v1'})
        binding = {'company_id': 'company', 'team_id': 'team', 'owner_ids': ['owner'],
                   'sequence_id': 'seq', 'account_id': 'account', 'public_id': 'sender'}
        task = {'id': 'task', 'type': 'linkedin_step_message', 'status': 'scheduled',
                'emailer_campaign_id': 'seq', 'user_id': 'owner', 'contact_id': 'contact',
                'contact': {'id': 'contact', 'linkedin_url': 'https://www.linkedin.com/in/person/'},
                'linkedin_emailer_template': {'body_text': 'Oi, teste.'}}
        reader = SimpleNamespace(team_id='team', owner_ids=frozenset(['owner']), get=Mock(return_value=task))
        self.handler.server.thunder_apollo_linkedin_attention = SimpleNamespace(reader=reader, team_id='team', company_id='company')
        self.handler.server.thunder_apollo_linkedin_authorized_company = 'company'
        resolver = Mock(return_value={'resolved': True, 'read_only': True, 'source': 'provider_observation',
            'target': {'account_id': 'account', 'account_public_id': 'sender',
                       'recipient_public_id': 'person', 'recipient_profile_urn': 'urn:li:fsd_profile:abc'}})
        self.handler.server.thunder_connectsafely_read_resolver = resolver
        repo = Mock(); repo.transaction = lambda: nullcontext(); repo.issue_apollo_linkedin_canary.return_value = {'id': 'auth'}
        self.handler.server.thunder_service.repository = repo
        authority = Mock(); authority.require.return_value = {'binding': binding}
        path = urlsplit('/api/os/v1/integrations/apollo/linkedin-canary')
        with patch('thunder_os.apollo_linkedin_roles.authorize_manager', return_value=True), \
             patch('thunder_os.apollo_linkedin_authority.CampaignApprovalAuthority', return_value=authority):
            self.assertEqual(self.handler.thunder_api(path), (201, {'id': 'auth'}))
            args, kw = repo.issue_apollo_linkedin_canary.call_args
            self.assertEqual(args[:4], ('company', 'task', 'account', 'person'))
            self.assertEqual(kw, {'action_type': 'linkedin_step_message', 'member_urn': 'urn:li:fsd_profile:abc',
                                  'campaign_version': 'v1', 'approval_id': 'approval', 'sequence_id': 'seq'})
            self.assertEqual(authority.require.call_count, 2)
            repo.issue_apollo_linkedin_canary.reset_mock()
            resolver.return_value['target']['account_id'] = 'other'
            with self.assertRaises(panel.Erro) as error: self.handler.thunder_api(path)
            self.assertEqual(error.exception.status, 503)
            repo.issue_apollo_linkedin_canary.assert_not_called()

    def test_invite_canary_is_bound_to_rendered_task_and_readonly_invite_target(self):
        from contextlib import nullcontext
        import hashlib
        task = {'id': 'task', 'type': 'linkedin_step_connect', 'status': 'scheduled',
                'emailer_campaign_id': 'seq', 'user_id': 'owner', 'contact_id': 'contact',
                'contact': {'id': 'contact', 'linkedin_url': 'https://www.linkedin.com/in/person/'},
                'linkedin_emailer_template': {'body_text': 'Oi, convite.'}}
        binding = {'company_id': 'company', 'team_id': 'team', 'owner_ids': ['owner'],
                   'sequence_id': 'seq', 'account_id': 'account', 'public_id': 'sender'}
        reader = SimpleNamespace(team_id='team', owner_ids=frozenset(['owner']), get=Mock(return_value=task))
        resolver = Mock(return_value={'resolved': True, 'read_only': True, 'source': 'provider_observation',
            'target': {'account_id': 'account', 'account_public_id': 'sender', 'action': 'invite',
                       'recipient_public_id': 'person', 'recipient_profile_urn': 'urn:li:fsd_profile:abc'}})
        server = SimpleNamespace(thunder_apollo_linkedin_attention=SimpleNamespace(
            reader=reader, team_id='team', company_id='company'), thunder_connectsafely_read_resolver=resolver)
        repo = Mock(); repo.transaction = lambda: nullcontext(); repo.issue_apollo_linkedin_canary.return_value = {'id': 'auth'}
        authority = Mock(); authority.require.return_value = {'binding': binding}
        body = {'company': 'company', 'task_id': 'task', 'campaign_id': 'campaign',
                'approval_id': 'approval', 'campaign_version': 'v1'}
        with patch('thunder_os.apollo_linkedin_authority.CampaignApprovalAuthority', return_value=authority):
            result = panel.issue_apollo_linkedin_panel_canary(server, repo, body, 'ygor')
        self.assertEqual(result, {'id': 'auth'})
        resolver.assert_called_once_with({'action': 'invite', 'profile_url': 'https://www.linkedin.com/in/person/'})
        args, kwargs = repo.issue_apollo_linkedin_canary.call_args
        self.assertEqual(args[4], hashlib.sha256('Oi, convite.'.encode()).hexdigest())
        self.assertEqual(kwargs['action_type'], 'linkedin_step_connect')
        self.assertEqual(kwargs['sequence_id'], 'seq')
        self.assertEqual(kwargs['member_urn'], 'urn:li:fsd_profile:abc')
        self.assertEqual(authority.require.call_count, 2)

    def test_canary_uses_observed_sequence_and_action_field_from_approval(self):
        from contextlib import nullcontext
        binding = {'company_id': 'company', 'team_id': 'team', 'owner_ids': ['owner'],
                   'sequence_ids': ['seq-one', 'seq-two'], 'account_id': 'account', 'public_id': 'sender'}
        task = {'id': 'task', 'type': 'linkedin_step_connect', 'status': 'scheduled',
                'emailer_campaign_id': 'seq-two', 'user_id': 'owner', 'contact_id': 'contact',
                'contact': {'id': 'contact', 'linkedin_url': 'https://www.linkedin.com/in/person/',
                            'typed_custom_fields': {'invite-field': 'Oi, convite.'}},
                'linkedin_emailer_template': {'body_text': 'Oi, convite.'}}
        reader = SimpleNamespace(team_id='team', owner_ids=frozenset(['owner']), get=Mock(return_value=task))
        resolver = Mock(return_value={'resolved': True, 'read_only': True, 'source': 'provider_observation',
            'target': {'account_id': 'account', 'account_public_id': 'sender', 'action': 'invite',
                       'recipient_public_id': 'person', 'recipient_profile_urn': 'urn:li:fsd_profile:abc'}})
        server = SimpleNamespace(thunder_apollo_linkedin_attention=SimpleNamespace(
            reader=reader, team_id='team', company_id='company'), thunder_connectsafely_read_resolver=resolver)
        repo = Mock(); repo.transaction = lambda: nullcontext(); repo.issue_apollo_linkedin_canary.return_value = {'id': 'auth'}
        authority = Mock(); authority.require.return_value = {'binding': binding, 'configuration': {
            'required_custom_field_ids': {'linkedin_step_connect': 'invite-field', 'linkedin_step_message': 'dm-field'}}}
        body = {'company': 'company', 'task_id': 'task', 'campaign_id': 'campaign',
                'approval_id': 'approval', 'campaign_version': 'v1'}
        with patch('thunder_os.apollo_linkedin_authority.CampaignApprovalAuthority', return_value=authority):
            self.assertEqual(panel.issue_apollo_linkedin_panel_canary(server, repo, body, 'ygor'), {'id': 'auth'})
            self.assertEqual(repo.issue_apollo_linkedin_canary.call_args.kwargs['sequence_id'], 'seq-two')
            repo.issue_apollo_linkedin_canary.reset_mock()
            task['emailer_campaign_id'] = 'outside'
            with self.assertRaises(panel.Erro):
                panel.issue_apollo_linkedin_panel_canary(server, repo, body, 'ygor')
            repo.issue_apollo_linkedin_canary.assert_not_called()

    def test_invite_canary_rejects_unresolved_overlong_and_wrong_target_action(self):
        from contextlib import nullcontext
        task = {'id': 'task', 'type': 'linkedin_step_connect', 'status': 'scheduled',
                'emailer_campaign_id': 'seq', 'user_id': 'owner', 'contact_id': 'contact',
                'contact': {'id': 'contact', 'linkedin_url': 'https://www.linkedin.com/in/person/'},
                'linkedin_emailer_template': {'body_text': 'hello'}}
        binding = {'company_id': 'company', 'team_id': 'team', 'owner_ids': ['owner'],
                   'sequence_id': 'seq', 'account_id': 'account', 'public_id': 'sender'}
        reader = SimpleNamespace(team_id='team', owner_ids=frozenset(['owner']), get=Mock(return_value=task))
        resolver = Mock(return_value={'resolved': True, 'read_only': True, 'source': 'provider_observation',
            'target': {'account_id': 'account', 'account_public_id': 'sender', 'action': 'invite',
                       'recipient_public_id': 'person', 'recipient_profile_urn': 'urn:li:fsd_profile:abc'}})
        server = SimpleNamespace(thunder_apollo_linkedin_attention=SimpleNamespace(
            reader=reader, team_id='team', company_id='company'), thunder_connectsafely_read_resolver=resolver)
        repo = Mock(); repo.transaction = lambda: nullcontext()
        authority = Mock(); authority.require.return_value = {'binding': binding}
        body = {'company': 'company', 'task_id': 'task', 'campaign_id': 'campaign',
                'approval_id': 'approval', 'campaign_version': 'v1'}
        with patch('thunder_os.apollo_linkedin_authority.CampaignApprovalAuthority', return_value=authority):
            for value in ('hello {{first_name}}', 'x' * 301, ''):
                with self.subTest(value=value):
                    task['linkedin_emailer_template']['body_text'] = value
                    with self.assertRaises(panel.Erro):
                        panel.issue_apollo_linkedin_panel_canary(server, repo, body, 'ygor')
            task['linkedin_emailer_template']['body_text'] = 'hello'
            resolver.return_value['target']['action'] = 'dm'
            with self.assertRaises(panel.Erro):
                panel.issue_apollo_linkedin_panel_canary(server, repo, body, 'ygor')
        repo.issue_apollo_linkedin_canary.assert_not_called()

    def test_invite_canary_route_requires_authorized_actor_and_persists_invite_type(self):
        from contextlib import nullcontext
        self.handler.command = 'POST'
        self.handler.thunder_proxy_actor = Mock(return_value='ygor')
        self.handler.thunder_actor = Mock(return_value='ygor')
        self.handler.corpo = Mock(return_value={'company': 'company', 'task_id': 'task',
            'campaign_id': 'campaign', 'approval_id': 'approval', 'campaign_version': 'v1'})
        binding = {'company_id': 'company', 'team_id': 'team', 'owner_ids': ['owner'],
                   'sequence_id': 'seq', 'account_id': 'account', 'public_id': 'sender'}
        task = {'id': 'task', 'type': 'linkedin_step_connect', 'status': 'scheduled',
                'emailer_campaign_id': 'seq', 'user_id': 'owner', 'contact_id': 'contact',
                'contact': {'id': 'contact', 'linkedin_url': 'https://www.linkedin.com/in/person/'},
                'linkedin_emailer_template': {'body_text': 'Olá'}}
        reader = SimpleNamespace(team_id='team', owner_ids=frozenset(['owner']), get=Mock(return_value=task))
        self.handler.server.thunder_apollo_linkedin_attention = SimpleNamespace(reader=reader, team_id='team', company_id='company')
        self.handler.server.thunder_apollo_linkedin_authorized_company = 'company'
        self.handler.server.thunder_connectsafely_read_resolver = Mock(return_value={
            'resolved': True, 'read_only': True, 'source': 'provider_observation',
            'target': {'account_id': 'account', 'account_public_id': 'sender', 'action': 'invite',
                       'recipient_public_id': 'person', 'recipient_profile_urn': 'urn:li:fsd_profile:abc'}})
        repo = Mock(); repo.transaction = lambda: nullcontext(); repo.issue_apollo_linkedin_canary.return_value = {'id': 'auth'}
        self.handler.server.thunder_service.repository = repo
        authority = Mock(); authority.require.return_value = {'binding': binding}
        with patch('thunder_os.apollo_linkedin_roles.authorize_manager', return_value=True), \
             patch('thunder_os.apollo_linkedin_authority.CampaignApprovalAuthority', return_value=authority):
            self.assertEqual(self.handler.thunder_api(urlsplit('/api/os/v1/integrations/apollo/linkedin-canary')),
                             (201, {'id': 'auth'}))
        self.assertEqual(repo.issue_apollo_linkedin_canary.call_args.kwargs['action_type'], 'linkedin_step_connect')

    def test_canary_denies_operator_and_protected_fixture(self):
        self.handler.command = 'POST'; self.handler.thunder_actor = Mock(return_value='ygor')
        self.handler.corpo = Mock(return_value={'company': 'company', 'task_id': '6aea584a2b8c4b0020f93935'})
        self.handler.server.thunder_apollo_linkedin_authorized_company = 'company'
        path = urlsplit('/api/os/v1/integrations/apollo/linkedin-canary')
        with patch('thunder_os.apollo_linkedin_roles.authorize_manager', return_value=False):
            with self.assertRaises(panel.Erro) as error: self.handler.thunder_api(path)
            self.assertEqual(error.exception.status, 403)
        with patch('thunder_os.apollo_linkedin_roles.authorize_manager', return_value=True):
            with self.assertRaises(panel.Erro) as error: self.handler.thunder_api(path)
            self.assertEqual(error.exception.status, 403)
        self.handler.server.thunder_service.repository.issue_apollo_linkedin_canary.assert_not_called()

    def test_canary_helper_protects_fixture_before_any_reader_call(self):
        from thunder_os.apollo_linkedin_attention import EXCLUDED_TASK
        repo = Mock()
        with self.assertRaises(panel.Erro) as error:
            panel.issue_apollo_linkedin_panel_canary(SimpleNamespace(), repo,
                {'task_id': EXCLUDED_TASK}, 'ygor')
        self.assertEqual(error.exception.status, 403)
        repo.assert_not_called()

    def test_canary_route_persists_and_consumes_real_sqlite_authorization(self):
        import tempfile
        from pathlib import Path
        from thunder_os.db import Repository, RepositoryConflict
        from thunder_os.campaign_registry import CampaignRegistry
        from thunder_os.service import ThunderService
        from test_thunder_os_apollo_linkedin_authority import config
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'panel.db')
            Path(path).touch()
            with patch.dict('os.environ', {'THUNDER_APOLLO_LI_OFFICIAL_DB': path}):
                repo = Repository(path)
                service = ThunderService(repo)
                cfg = config(); CampaignRegistry(repo).register(cfg, 'ygor', 0)
                repo.put_state('control', 'global', {'paused': False, 'actor': 'ygor'})
                review = service.apollo_campaign_approval_review('campaign')
                approval = service.approve_apollo_campaign_version('campaign', 'ygor',
                    execution_confirmation=review['execution_confirmation'])
                reader = SimpleNamespace(team_id='team', owner_ids=frozenset(['owner']), get=lambda _: {
                    'id': 'task', 'type': 'linkedin_step_message', 'status': 'scheduled',
                    'emailer_campaign_id': 'seq', 'user_id': 'owner', 'contact_id': 'contact',
                    'contact': {'id': 'contact', 'linkedin_url': 'https://linkedin.com/in/person'},
                    'linkedin_emailer_template': {'body_text': 'Teste'}})
                self.handler.server.thunder_service = service
                self.handler.server.thunder_apollo_linkedin_attention = SimpleNamespace(reader=reader, team_id='team', company_id='company')
                self.handler.server.thunder_apollo_linkedin_authorized_company = 'company'
                self.handler.server.thunder_connectsafely_read_resolver = lambda _: {
                    'resolved': True, 'read_only': True, 'source': 'provider_observation',
                    'target': {'account_id': 'a'*24, 'account_public_id': 'sender',
                               'recipient_public_id': 'person', 'recipient_profile_urn': 'urn:li:fsd_profile:abc'}}
                self.handler.command = 'POST'; self.handler.thunder_actor = Mock(return_value='ygor')
                self.handler.corpo = Mock(return_value={'company': 'company', 'task_id': 'task',
                    'campaign_id': 'campaign', 'approval_id': approval['id'], 'campaign_version': 'v1'})
                with patch('thunder_os.apollo_linkedin_roles.authorize_manager', return_value=True):
                    status, authorization = self.handler.thunder_api(urlsplit('/api/os/v1/integrations/apollo/linkedin-canary'))
                self.assertEqual(status, 201)
                persisted = repo.get_state('apollo_linkedin_canary', authorization['id'])
                self.assertEqual((persisted['state'], persisted['campaign_version'], persisted['member_urn']),
                                 ('issued', 'v1', 'urn:li:fsd_profile:abc'))
                args = (authorization['id'], 'company', 'task', 'linkedin_step_message', 'a'*24,
                        'person', persisted['text_hash'], 'v1')
                kwargs = {'member_urn': 'urn:li:fsd_profile:abc', 'approval_id': approval['id'], 'sequence_id': 'seq'}
                repo.reserve_apollo_linkedin_with_canary(*args, **kwargs)
                self.assertEqual(repo.get_state('apollo_linkedin_canary', authorization['id'])['state'], 'consumed')
                with self.assertRaises(RepositoryConflict): repo.reserve_apollo_linkedin_with_canary(*args, **kwargs)

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
        import hashlib
        from thunder_os.apollo_linkedin_attention import ApolloLinkedInAttention
        from thunder_os.apollo_linkedin_executor import DryRunExecutor
        from thunder_os.apollo_linkedin_reader import CampaignBindings
        task = {'id': 'test-task', 'type': 'linkedin_step_connect', 'status': 'scheduled',
                'emailer_campaign_id': 'campaign', 'user_id': 'owner',
                'linkedin_emailer_template': {'body_text': 'Olá, lead!'}}
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
        self.assertEqual(body['items'][0]['text_hash'], hashlib.sha256('Olá, lead!'.encode()).hexdigest())
        self.assertFalse(body['items'][0]['execution_enabled'])
        account_status.assert_called_once_with('account')

        for value, expected in ((None, 'text_missing'), ('{{name}}', 'text_unresolved_variables'), ('x' * 301, 'text_too_long')):
            with self.subTest(reason=expected):
                task['linkedin_emailer_template'] = {'body_text': value}
                item = self.request()[1]['items'][0]
                self.assertEqual(item['reason'], expected)
                self.assertIsNone(item['text_hash'])
                self.assertFalse(item['execution_enabled'])

    def test_deadline_returns_partial_even_if_reader_hangs(self):
        import threading
        from thunder_os.apollo_linkedin_attention import ApolloLinkedInAttention
        gate = threading.Event()
        reader = Mock(); reader.list_open_tasks.side_effect = lambda: gate.wait(2)
        service = SimpleNamespace(list_apollo_linkedin_reservations=lambda: [{'company': 'c', 'task_id': 'orphan', 'state': 'reserved'}], list_apollo_linkedin_account_locks=lambda: [])
        attention = ApolloLinkedInAttention(service, reader, Mock(), company_id='c', team_id='t', budget_seconds=.02)
        try:
            result = attention.snapshot()
            self.assertEqual(result['reason'], 'time_budget_exhausted')
            self.assertEqual(result['items'][0]['task_id'], 'orphan')
            self.assertEqual(attention.snapshot()['reason'], 'read_in_progress')
        finally:
            gate.set()

if __name__ == '__main__': unittest.main()
