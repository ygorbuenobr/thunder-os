"""LinkedIn authorization screen support; SQLite and fakes only, no provider IO."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import painel_servidor as panel
from thunder_os import linkedin_authorization as li_auth
from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.connectsafely_application import ConnectSafelyApplication
from thunder_os.connectsafely_console import ConnectSafelyConsole, ConnectSafelyError
from thunder_os.db import Repository
from thunder_os.hermes_linkedin_cadence import CadenceBlocked, STEP_CONTRACT, validate_delegated_plan
from thunder_os.hermes_linkedin_runtime import task_cadence_binding
from thunder_os.service import AuthorizationError
from test_campaign_sheet_support import make_sheet

ACCOUNT = 'a' * 24
SENDER = {'account_id': ACCOUNT, 'public_id': 'fixture-owner',
          'profile_url': 'https://www.linkedin.com/in/fixture-owner/', 'source': 'direct_runtime_live',
          'identity_verifier_contacts': ['258958235509']}
TEXTS = {'cad_li_0': '', 'cad_li_1': 'Oi Laura, teste do Thunder OS.\nLinha 2.',
         'cad_li_2': 'Segunda mensagem', 'cad_li_3': 'Terceira mensagem'}
URN = 'urn:li:fsd_profile:ACoAADfwURQBEDaHhGr7YJr53N6VvhahAPfkQr0'


def resolver(calls=None, **overrides):
    def resolve(body):
        if calls is not None:
            calls.append(body)
        result = {'resolved': True, 'read_only': True, 'actions_available': False,
                  'target': {'account_id': ACCOUNT, 'account_public_id': 'fixture-owner', 'channel': 'linkedin',
                             'action': 'dm', 'recipient_public_id': 'laura-myslinsky-330373222',
                             'recipient_profile_urn': URN, 'conversation_urn': None,
                             'messaging_channel': 'linkedin_inbox'},
                  'preview': {'label': 'Laura Myslinsky'}}
        result['target'].update(overrides)
        return result
    return resolve


class FakeHubSpot:
    def contact(self, ident):
        return {'id': ident, 'name': 'Laura Myslinsky', 'company': 'Clínica',
                'linkedin_url': 'https://www.linkedin.com/in/laura-myslinsky-330373222/', 'cad_status': 'aprovado'}

    def open_tasks(self, ident):
        return {'read_only': True, 'contact_id': ident, 'complete': True,
                'tasks': [{'id': '999', 'subject': 'LinkedIn: msg 1', 'body': 'cad_li_1', 'status': 'NOT_STARTED',
                           'due_at': '2026-11-03T12:00:00Z', 'marker': 'cad_li_1', 'contract_ok': True}]}


class VerifiedClient:
    def verify_identity(self, *, for_write=False):
        return {'verified': True, 'account_id': ACCOUNT, 'account_public_id': 'fixture-owner'}


class LinkedInAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name) / 'state.sqlite')
        self.now = time.time()
        self.registry = CampaignRegistry(self.repo)

    def snapshot(self):
        with self.repo.transaction() as con:
            return con.execute('SELECT kind, entity_id, version, payload FROM thunder_state ORDER BY 1, 2').fetchall()

    def draft(self, body=None, **kwargs):
        body = body or {'hubspot_contact_id': '258958235509', 'profile': 'laura-myslinsky-330373222',
                        'texts': dict(TEXTS), 'valid_hours': 72}
        options = {'sender': SENDER, 'resolver': resolver(), 'hubspot': FakeHubSpot(),
                   'repository': self.repo, 'clock': lambda: self.now}
        options.update(kwargs)
        return li_auth.draft_approval(body, **options)

    def handler(self, method, *, actor='ygor', body=None, app=None, trusted=True, server_extra=None):
        handler = object.__new__(panel.Handler)
        service = SimpleNamespace(repository=self.repo, campaign_registry=self.registry)
        attributes = {'thunder_service': service, 'thunder_connectsafely_app': app,
                      'thunder_origin': 'http://localhost',
                      'thunder_nonces': {'test-nonce': (actor, time.monotonic() + 30)},
                      'thunder_connectsafely_read_resolver': resolver(),
                      'thunder_linkedin_hubspot_reader': FakeHubSpot()}
        handler.server = SimpleNamespace(**{**attributes, **(server_extra or {})})
        handler.command = method
        handler.client_address = ('127.0.0.1', 12345)
        handler.headers = {'Origin': 'http://localhost', 'X-Thunder-Nonce': 'test-nonce', 'X-Authenticated-User': actor}
        handler.corpo = lambda: body
        handler.responder = lambda status, value: (status, value)
        handler.trusted_confirmation_peer = lambda: trusted
        return handler

    def call(self, path, method='GET', **kwargs):
        with patch.object(panel, 'restricted_mode', return_value=False):
            return self.handler(method, **kwargs).thunder_api(urlsplit(path))

    # Draft builder -----------------------------------------------------------

    def test_draft_is_exact_approval_body_and_persists_nothing(self):
        calls = []
        before = self.snapshot()
        result = self.draft(resolver=resolver(calls))
        self.assertEqual(before, self.snapshot())
        self.assertEqual([{'action': 'dm', 'profile_url': 'https://www.linkedin.com/in/laura-myslinsky-330373222/'}], calls)
        approval = result['approval']
        self.assertEqual({'id', 'hubspot_contact_id', 'cadence_id', 'expires_at', 'steps'}, set(approval))
        self.assertEqual(sorted(STEP_CONTRACT), [step['marker'] for step in approval['steps']])
        for step in approval['steps']:
            subject, action = STEP_CONTRACT[step['marker']]
            self.assertEqual(subject, step['subject'])
            self.assertEqual(action, step['plan']['action'])
            self.assertEqual(TEXTS[step['marker']], step['plan']['content'])
            self.assertEqual(URN, step['plan']['recipient_profile_urn'])
            self.assertEqual((ACCOUNT, 'fixture-owner'), (step['plan']['account_id'], step['plan']['account_public_id']))
            self.assertIsNone(step['plan']['conversation_urn'])
        self.assertEqual('connectsafely_profile_read', result['recipient']['urn_source'])
        self.assertEqual('fixture-owner', result['sender']['public_id'])
        self.assertEqual('aprovado', result['hubspot_contact']['cad_status'])
        self.assertFalse(result['persisted'])

    def test_drafted_approval_matches_executor_preview_hash(self):
        result = self.draft()
        app = li_auth.validation_application(self.repo, SENDER)
        from thunder_os.hermes_linkedin_cadence import HermesLinkedInCadence
        approved = HermesLinkedInCadence(self.repo, None, app).approve(result['approval'], 'ygor')
        self.assertEqual('active', approved['status'])
        self.repo.put_state('delegation', 'hermes', {'enabled': True, 'scopes': ['linkedin_cadence'], 'daily_limit': 1})
        self.repo.put_state('agent_control', 'global', {'enabled': True, 'paused': False})
        console = ConnectSafelyConsole(self.repo, VerifiedClient(), None, account_id=ACCOUNT,
                                       account_public_id='fixture-owner', clock=lambda: self.now)
        step = next(s for s in approved['steps'] if s['marker'] == 'cad_li_1')
        preview = console.prepare(step['plan'], actor='ygor')
        authority = {'id': approved['id'], 'version': approved['version'],
                     'review_hash': approved['review_hash'], 'marker': 'cad_li_1'}
        self.assertEqual(approved['id'], validate_delegated_plan(self.repo, authority, preview['plan'],
                                                                 lambda: self.now)['id'])
        self.assertEqual(result['plan_hashes']['cad_li_1'], step['plan_hash'])
        self.assertEqual(result['approval_hash_preview'], approved['review_hash'])

    def test_draft_fails_closed(self):
        cases = [
            ({'resolver': resolver(account_id='b' * 24)}, 'sender_identity_mismatch'),
            ({'resolver': lambda body: {'resolved': False, 'error_code': 'profile_quota_exhausted'}}, 'profile_quota_exhausted'),
            ({'resolver': None}, 'profile_reader_unavailable'),
        ]
        for kwargs, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(li_auth.AuthorizationReadError, code):
                self.draft(**kwargs)
        base = {'hubspot_contact_id': '258958235509', 'profile': 'laura-myslinsky-330373222',
                'texts': dict(TEXTS), 'valid_hours': 72}
        for body, code in (
                ({**base, 'texts': {**TEXTS, 'cad_li_1': '   '}}, 'cad_li_1:invalid_content'),
                ({**base, 'texts': {**TEXTS, 'cad_li_0': 'x' * 301}}, 'cad_li_0:invalid_content'),
                ({**base, 'texts': {'cad_li_1': 'só um'}}, 'draft_texts_invalid'),
                ({**base, 'valid_hours': 0}, 'draft_validity_invalid'),
                ({**base, 'valid_hours': 169}, 'draft_validity_invalid'),
                ({**base, 'hubspot_contact_id': '01'}, 'contact_id_invalid'),
                ({**base, 'extra': True}, 'draft_invalid')):
            with self.subTest(code=code), self.assertRaisesRegex(li_auth.AuthorizationReadError, code):
                self.draft(body)
        self.assertEqual([], self.snapshot())

    # Panel routes ------------------------------------------------------------

    def test_state_route_is_read_only_and_shows_blockers(self):
        with patch.object(li_auth, 'configured_sender', return_value=SENDER):
            before = self.snapshot()
            status, state = self.call('/api/os/v1/linkedin-authorization?contact_id=258958235509')
            self.assertEqual(before, self.snapshot())
        self.assertEqual(200, status)
        self.assertTrue(state['read_only'])
        self.assertEqual('fixture-owner', state['sender']['public_id'])
        self.assertEqual({'connectsafely_console_unavailable', 'delegation_off', 'agent_paused',
                          'campaign_unavailable'}, set(state['blockers']))
        self.assertIsNone(state['delegation'])
        with patch.object(li_auth, 'configured_sender',
                          side_effect=li_auth.AuthorizationReadError('sender_config_unavailable')):
            _, state = self.call('/api/os/v1/linkedin-authorization?contact_id=111')
        self.assertIn('sender_config_unavailable', state['blockers'])
        self.assertIsNone(state['sender'])

    def test_state_route_rejects_other_actors_and_bad_queries(self):
        with self.assertRaises((panel.Erro, AuthorizationError)):
            self.call('/api/os/v1/linkedin-authorization?contact_id=1', actor='claude')
        with self.assertRaises(panel.Erro):
            self.call('/api/os/v1/linkedin-authorization?contact_id=1', trusted=False)
        for query in ('', 'contact_id=abc', 'contact_id=1&contact_id=2', 'contact_id=1&x=1'):
            with self.subTest(query=query), self.assertRaises(panel.Erro):
                self.call('/api/os/v1/linkedin-authorization?' + query)

    def test_tasks_route_uses_read_only_reader(self):
        status, result = self.call('/api/os/v1/linkedin-authorization/tasks?contact_id=258958235509')
        self.assertEqual(200, status)
        self.assertEqual(['999'], [task['id'] for task in result['tasks']])
        with self.assertRaises(panel.Erro) as failure, patch.dict('os.environ', {'THUNDER_OS_HUBSPOT_READ_ENABLED': ''}):
            self.call('/api/os/v1/linkedin-authorization/tasks?contact_id=1',
                      server_extra={'thunder_linkedin_hubspot_reader': None})
        self.assertEqual(503, failure.exception.status)

    def test_draft_route_persists_nothing_and_requires_ygor(self):
        body = {'hubspot_contact_id': '258958235509', 'profile': 'https://www.linkedin.com/in/laura-myslinsky-330373222/',
                'texts': dict(TEXTS), 'valid_hours': 24}
        with patch.object(li_auth, 'configured_sender', return_value=SENDER):
            status, result = self.call('/api/os/v1/linkedin-cadence-plan-drafts', 'POST', body=body)
            self.assertEqual(200, status)
            self.assertEqual([], self.snapshot())
            with self.assertRaises((panel.Erro, AuthorizationError)):
                self.call('/api/os/v1/linkedin-cadence-plan-drafts', 'POST', body=body, actor='hermes')
            with self.assertRaises(panel.Erro) as failure:
                self.call('/api/os/v1/linkedin-cadence-plan-drafts', 'POST', body={**body, 'valid_hours': 0})
        self.assertEqual(409, failure.exception.status)
        self.assertIn('draft_validity_invalid', str(failure.exception))
        self.assertEqual(4, len(result['approval']['steps']))

    def test_approval_without_live_console_uses_validation_only_console_and_revokes(self):
        with patch.object(li_auth, 'configured_sender', return_value=SENDER):
            draft = self.draft()['approval']
            status, approved = self.call('/api/os/v1/linkedin-cadence-approvals', 'POST', body=draft,
                                         app=ConnectSafelyApplication(self.repo))
        self.assertEqual(201, status)
        self.assertEqual('active', approved['status'])
        self.assertEqual([], self.repo.list_state('connectsafely_preview'))
        self.assertEqual([], self.repo.list_state('connectsafely_job'))
        with patch.object(li_auth, 'configured_sender', return_value={**SENDER, 'public_id': 'other-owner'}), \
                self.assertRaises(ConnectSafelyError):
            self.call('/api/os/v1/linkedin-cadence-approvals', 'POST', body={**draft, 'id': 'other'})
        binding = {'task_id': '999', 'approval_id': approved['id'], 'approval_version': approved['version'],
                   'review_hash': approved['review_hash'], 'marker': 'cad_li_1'}
        app = ConnectSafelyApplication(self.repo)
        status, _ = self.call('/api/os/v1/linkedin-cadence-task-bindings', 'POST', body=binding, app=app)
        self.assertEqual(201, status)
        self.assertEqual(approved['cadence_id'], task_cadence_binding(self.repo, '999'))
        with self.assertRaises(Exception):
            self.call('/api/os/v1/linkedin-cadence-approvals/revoke', 'POST',
                      body={'approval_id': approved['id'], 'expected_version': approved['version'] + 1})
        with self.assertRaises((panel.Erro, AuthorizationError, CadenceBlocked)):
            self.call('/api/os/v1/linkedin-cadence-approvals/revoke', 'POST', actor='hermes',
                      body={'approval_id': approved['id'], 'expected_version': approved['version']})
        status, revoked = self.call('/api/os/v1/linkedin-cadence-approvals/revoke', 'POST',
                                    body={'approval_id': approved['id'], 'expected_version': approved['version']})
        self.assertEqual((200, 'revoked', 'ygor'), (status, revoked['status'], revoked['revoked_by']))
        self.assertIsNone(task_cadence_binding(self.repo, '999'))
        self.repo.put_state('delegation', 'hermes', {'enabled': True, 'scopes': ['linkedin_cadence'], 'daily_limit': 1})
        self.repo.put_state('agent_control', 'global', {'enabled': True, 'paused': False})
        step = draft['steps'][1]
        with self.assertRaisesRegex(ConnectSafelyError, 'delegated_authority_changed'):
            validate_delegated_plan(self.repo, {'id': approved['id'], 'version': approved['version'],
                                                'review_hash': approved['review_hash'], 'marker': 'cad_li_1'},
                                    step['plan'], time.time)
        with patch.object(li_auth, 'configured_sender', return_value=SENDER):
            _, state = self.call('/api/os/v1/linkedin-authorization?contact_id=258958235509')
        self.assertEqual(['revoked'], [a['status'] for a in state['approvals']])
        self.assertEqual('999', state['tasks'][0]['task_id'])

    def test_approval_without_console_or_config_stays_unavailable(self):
        with self.assertRaises(panel.Erro) as failure, patch.dict('os.environ', {'THUNDER_OS_CONNECTSAFELY_CONFIG': ''}):
            self.call('/api/os/v1/linkedin-cadence-approvals', 'POST', body={}, app=None)
        self.assertEqual(503, failure.exception.status)

    def test_campaign_options_require_thunder_os_connectsafely_account(self):
        store, sheet = make_sheet(Path(self.temp.name) / 'sheet')
        def register(ident, platform, provider, account):
            self.registry.register({'campaign_id': ident, 'campaign_version': '1', 'rules_version': '1',
                'run_id': ident, 'platform': platform, 'providers': {'linkedin': provider}, 'rules': 'x',
                'briefing': sheet, 'configuration': {'flow_version': '1', 'provider_accounts': {'linkedin': account}}},
                'ygor', 0)
        register('ok', 'thunder_os', 'connectsafely', ACCOUNT)
        register('other-platform', 'codex_local', 'connectsafely', ACCOUNT)
        register('other-account', 'thunder_os', 'connectsafely', 'b' * 24)
        options = {row['id']: row for row in li_auth.campaign_options(self.registry, SENDER)}
        self.assertTrue(options['ok']['eligible'])
        self.assertEqual('campaign_platform_not_thunder_os', options['other-platform']['reason'])
        self.assertEqual('campaign_account_mismatch', options['other-account']['reason'])


    def test_revoke_refused_while_task_in_flight(self):
        from thunder_os.hermes_linkedin_cadence import revoke_approval, HermesLinkedInCadence
        approved = HermesLinkedInCadence(self.repo, None, li_auth.validation_application(self.repo, SENDER)).approve(
            self.draft()['approval'], 'ygor')
        for status in ('pending', 'uncertain', 'provider_confirmed'):
            self.repo.put_state('linkedin_cadence_task', '999', {'status': status, 'approval_id': approved['id']})
            with self.subTest(status=status), self.assertRaisesRegex(CadenceBlocked, 'approval_has_task_in_flight'):
                revoke_approval(self.repo, approved['id'], approved['version'], 'ygor')
        self.assertEqual('active', self.repo.get_state('linkedin_cadence_approval', approved['id'])['status'])
        self.repo.put_state('linkedin_cadence_task', '999', {'status': 'completed', 'approval_id': approved['id']})
        self.assertEqual('revoked', revoke_approval(self.repo, approved['id'], approved['version'], 'ygor')['status'])
        with self.assertRaises(Exception):
            HermesLinkedInCadence(self.repo, None, li_auth.validation_application(self.repo, SENDER)).approve(
                {k: v for k, v in self.draft()['approval'].items() if k != 'id'} | {'id': approved['id']}, 'ygor')

    def test_configured_sender_requires_panel_live_admission(self):
        live = {'account_id': ACCOUNT, 'public_id': 'fixture-owner', 'social_enabled': False,
                'execution_scope': 'scope-1', 'canary_readers': {'contacts': [
                    {'hubspot_contact_id': '258958235509', 'channels': ['linkedin']}]}}
        base = {'version': 1, 'mode': 'live', 'factory': 'builtin-connectsafely-v1', 'execution_enabled': True,
                'repository': str(Path(self.temp.name) / 'state.sqlite'), 'live': live}
        import hashlib
        def sender(cfg):
            path = Path(self.temp.name) / 'cfg.json'
            raw = json.dumps(cfg).encode(); path.write_bytes(raw)
            return li_auth.configured_sender({'THUNDER_OS_CONNECTSAFELY_CONFIG': str(path),
                                              'THUNDER_OS_CONNECTSAFELY_CONFIG_SHA256': hashlib.sha256(raw).hexdigest()})
        result = sender(base)
        self.assertEqual(('fixture-owner', ['258958235509']), (result['public_id'], result['identity_verifier_contacts']))
        for change in ({'mode': 'synthetic'}, {'mode': 'disabled'}, {'factory': 'other'},
                       {'live': {**live, 'execution_scope': ''}}, {'live': {**live, 'social_enabled': 'no'}}):
            with self.subTest(change=change), self.assertRaisesRegex(li_auth.AuthorizationReadError, 'sender_config_unavailable'):
                sender({**base, **change})
        path = Path(self.temp.name) / 'cfg.json'
        with self.assertRaisesRegex(li_auth.AuthorizationReadError, 'sender_config_changed'):
            li_auth.configured_sender({'THUNDER_OS_CONNECTSAFELY_CONFIG': str(path),
                                       'THUNDER_OS_CONNECTSAFELY_CONFIG_SHA256': '0' * 64})

    def test_draft_refuses_sender_as_recipient_and_campaigns_need_sender(self):
        with self.assertRaisesRegex(li_auth.AuthorizationReadError, 'recipient_is_sender'):
            self.draft(resolver=resolver(recipient_public_id='Fixture-Owner'))
        store, sheet = make_sheet(Path(self.temp.name) / 'sheet2')
        self.registry.register({'campaign_id': 'no-account', 'campaign_version': '1', 'rules_version': '1',
            'run_id': 'r', 'platform': 'thunder_os', 'providers': {'linkedin': 'connectsafely'}, 'rules': 'x',
            'briefing': sheet, 'configuration': {'flow_version': '1', 'provider_accounts': {'linkedin': ACCOUNT}}}, 'ygor', 0)
        self.assertEqual([('no-account', False, 'sender_unavailable')],
                         [(o['id'], o['eligible'], o['reason']) for o in li_auth.campaign_options(self.registry, {})])


class HubSpotCadenceReaderTests(unittest.TestCase):
    def reader(self, responses, calls):
        def request(method, path, body, headers, timeout):
            calls.append((method, path, body))
            return responses.pop(0)
        return li_auth.HubSpotCadenceReader(lambda: 'token', request=request)

    def test_open_tasks_filters_contact_and_flags_contract(self):
        calls = []
        rows = [{'id': '1', 'properties': {'hs_task_subject': 'LinkedIn: msg 1', 'hs_task_body': 'cad_li_1',
                                           'hs_task_status': 'NOT_STARTED', 'hs_timestamp': 'x'}},
                {'id': '2', 'properties': {'hs_task_subject': 'LinkedIn: msg 2', 'hs_task_body': 'texto livre',
                                           'hs_task_status': 'NOT_STARTED', 'hs_timestamp': 'y'}}]
        reader = self.reader([(200, json.dumps({'portalId': 51412240}).encode()),
                              (200, json.dumps({'results': rows}).encode())], calls)
        result = reader.open_tasks('258958235509')
        self.assertEqual([True, False], [task['contract_ok'] for task in result['tasks']])
        self.assertEqual(['POST'], [method for method, path, _ in calls if 'tasks' in path])
        search = calls[1][2]
        self.assertEqual(4, len(search['filterGroups']))
        for group in search['filterGroups']:
            self.assertIn({'propertyName': 'associations.contact', 'operator': 'EQ', 'value': '258958235509'},
                          group['filters'])
        self.assertTrue(result['complete'])

    def test_portal_mismatch_and_write_routes_blocked(self):
        reader = self.reader([(200, json.dumps({'portalId': 1}).encode())], [])
        with self.assertRaisesRegex(li_auth.AuthorizationReadError, 'hubspot_portal_mismatch'):
            reader.open_tasks('1')
        for method, path in (('PATCH', '/crm/v3/objects/tasks/1'), ('POST', '/crm/v3/objects/tasks'),
                             ('DELETE', '/crm/v3/objects/contacts/1')):
            with self.subTest(method=method), self.assertRaisesRegex(li_auth.AuthorizationReadError, 'hubspot_endpoint_blocked'):
                reader._call(method, path, {})


if __name__ == '__main__':
    unittest.main()
