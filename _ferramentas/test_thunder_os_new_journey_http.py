"""New journey routing with isolated loopback and no provider connection."""
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

import painel_servidor as panel
from thunder_os.db import RepositoryConflict
from thunder_os.models import ContractError
from test_thunder_os_new_journey import StaticTemplates, passing


class NewJourneyHTTPTests(unittest.TestCase):
    def setUp(self):
        self.wizard = Mock()
        for name in ('catalog', 'create', 'get', 'source', 'check', 'sequences', 'revise_sequence',
                     'approve_step', 'export', 'reconcile', 'qualify'):
            getattr(self.wizard, name).return_value = {'enrollment_enabled': False, 'id': 'draft-test'}
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        self.server.banco = SimpleNamespace()
        self.server.thunder_service = Mock()
        self.server.thunder_new_journey = self.wizard
        self.server.thunder_origin = 'https://panel.example.invalid'
        self.server.thunder_nonces = {}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, suffix, *, actor='ygor', method='GET', body=None, nonce=False, origin=None):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.addCleanup(connection.close)
        headers = {'X-Authenticated-User': actor}
        if nonce:
            token = str(time.monotonic_ns())
            self.server.thunder_nonces[token] = (actor, time.monotonic() + 30)
            headers.update({'X-Thunder-Nonce': token, 'Origin': origin or self.server.thunder_origin})
        if body is not None:
            headers['Content-Type'] = 'application/json'
        connection.request(method, '/api/os/v1/new-journey/' + suffix,
                           body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def test_N09_only_authenticated_ygor_reads_catalog_and_drafts(self):
        self.assertEqual(self.request('catalog')[0], 200)
        self.assertEqual(self.request('draft-test')[0], 200)
        self.wizard.catalog.assert_called_once_with()
        self.wizard.get.assert_called_once_with('draft-test', 'ygor')
        for actor, expected in [('', 401), ('codex', 403), ('hermes', 401)]:
            self.assertEqual(self.request('catalog', actor=actor)[0], expected)

    def test_N09_mutations_require_nonce_origin_and_ygor(self):
        body = {'campaign_id': 'BR-APOLLO-01', 'name': 'New draft'}
        self.assertEqual(self.request('create', method='POST', body=body)[0], 401)
        self.assertEqual(self.request('create', method='POST', body=body, nonce=True,
                                      origin='https://attacker.example.invalid')[0], 401)
        self.assertEqual(self.request('create', actor='codex', method='POST', body=body, nonce=True)[0], 403)
        self.wizard.create.assert_not_called()
        self.assertEqual(self.request('create', method='POST', body=body, nonce=True)[0], 200)
        self.wizard.create.assert_called_once_with(body, 'ygor')

    def test_N09_strict_fields_versions_paths_and_methods(self):
        for suffix in ('../private', 'draft%2Fprivate', 'draft-test?campaign=MX27-CDMX'):
            self.assertIn(self.request(suffix)[0], (400, 404))
        for method in ('PUT', 'PATCH', 'DELETE'):
            self.assertEqual(self.request('draft-test/check', method=method)[0], 405)
        for body in ({'expected_version': True}, {'expected_version': 0},
                     {'expected_version': 1, 'dispatch': True}):
            self.assertEqual(self.request('draft-test/check', method='POST', body=body, nonce=True)[0], 400)
        self.wizard.check.assert_not_called()

    def test_N03_source_modes_do_not_accept_paid_or_arbitrary_routes(self):
        for extra in ({'mode': 'enrichment', 'email': 'person@example.invalid'},
                      {'mode': 'apollo_people', 'filters': {}, 'page': 1, 'endpoint': '/people/match'},
                      {'mode': 'csv', 'csv_text': '', 'verified': True}):
            status, _ = self.request('draft-test/source', method='POST', nonce=True,
                                     body={'expected_version': 2, **extra})
            self.assertEqual(status, 400)
        self.wizard.source.assert_not_called()
        for data in ({'mode': 'csv', 'csv_text': 'email\nperson@example.invalid'},
                     {'mode': 'apollo_people', 'filters': {'person_titles': ['CMO']}, 'page': 1},
                     {'mode': 'apollo_contacts', 'query': 'company', 'page': 1}):
            self.assertEqual(self.request('draft-test/source', method='POST', nonce=True,
                body={'expected_version': 2, **data})[0], 200)
            self.wizard.source.assert_called_with('draft-test', data, 'ygor', expected_version=2)

    def test_qualification_uses_same_actor_nonce_origin_and_exact_schema(self):
        body = {'expected_version': 2, 'source_key': 'csv:1', 'facts': {}, 'review_hash': 'a' * 64}
        for actor, nonce, expected in [('ygor', False, 401), ('codex', True, 403)]:
            self.assertEqual(self.request('draft-test/qualify', actor=actor, method='POST',
                                          body=body, nonce=nonce)[0], expected)
        self.wizard.qualify.assert_not_called()
        self.assertEqual(self.request('draft-test/qualify', method='POST', nonce=True,
                                      body={**body, 'write_hubspot': True})[0], 400)
        self.assertEqual(self.request('draft-test/qualify', method='POST', nonce=True, body=body)[0], 200)
        self.wizard.qualify.assert_called_once_with('draft-test', 'csv:1', {}, 'ygor',
                                                   expected_version=2, review_hash='a' * 64)

    def test_N06_exact_step_revision_and_approval_never_dispatch(self):
        steps = [{'id': 'D0', 'channel': 'email', 'subject': 'Original', 'content': 'Exact text',
                  'order': 1, 'interval_hours': 0}]
        self.assertEqual(self.request('draft-test/sequence', method='POST', nonce=True,
            body={'expected_version': 2, 'steps': steps})[0], 200)
        self.wizard.revise_sequence.assert_called_once_with('draft-test', steps, 'ygor', expected_version=2)
        body = {'expected_version': 3, 'review_hash': 'a' * 64, 'step_id': 'D0'}
        self.assertEqual(self.request('draft-test/approve-step', method='POST', nonce=True, body=body)[0], 200)
        self.wizard.approve_step.assert_called_once_with('draft-test', 'D0', 'ygor',
            expected_version=3, review_hash='a' * 64)
        self.server.thunder_service.dispatch_step.assert_not_called()
        self.server.thunder_service.control_agent.assert_not_called()

    def test_N08_sequence_selection_uses_read_catalog_and_exact_external_id(self):
        self.assertEqual(self.request('draft-test/sequences', method='POST', nonce=True,
            body={'expected_version': 2, 'query': 'Existing sequence'})[0], 200)
        self.wizard.sequences.assert_called_once_with('draft-test', 'Existing sequence', 'ygor', expected_version=2)
        data = {'expected_version': 2, 'steps': [], 'apollo_sequence_id': 'sequence-test',
                'apollo_sequence_query': 'Existing sequence'}
        self.assertEqual(self.request('draft-test/sequence', method='POST', nonce=True, body=data)[0], 200)
        self.wizard.revise_sequence.assert_called_once_with('draft-test', [], 'ygor',
            expected_version=2, apollo_sequence_id='sequence-test', apollo_sequence_query='Existing sequence')
        del data['apollo_sequence_query']
        self.assertEqual(self.request('draft-test/sequence', method='POST', nonce=True, body=data)[0], 400)

    def test_N07_export_and_reconciliation_route_only_to_manual_workflow(self):
        self.wizard.export.return_value = {'filename': 'draft.csv', 'csv_text': 'email\n',
                                          'rows': 0, 'credits': 0, 'enrollment_enabled': False}
        status, result = self.request('draft-test/export', method='POST', nonce=True,
            body={'expected_version': 3, 'review_hash': 'b' * 64})
        self.assertEqual(status, 200)
        self.assertFalse(result['enrollment_enabled'])
        self.assertEqual(result['credits'], 0)
        self.assertEqual(self.request('draft-test/reconcile', method='POST', nonce=True,
            body={'expected_version': 3})[0], 200)
        self.wizard.reconcile.assert_called_once_with('draft-test', 'ygor', expected_version=3)
        self.assertEqual(self.request('draft-test/enroll', method='POST', nonce=True,
            body={'expected_version': 3})[0], 404)
        self.server.thunder_service.dispatch_step.assert_not_called()

    def test_N09_errors_hide_internal_data_and_conflicts_require_refresh(self):
        self.wizard.get.side_effect = ContractError('private payload token-like-data')
        status, result = self.request('draft-test')
        self.assertEqual(status, 409)
        self.assertNotIn('token-like-data', json.dumps(result))
        self.wizard.check.side_effect = RepositoryConflict('private writer data')
        self.assertEqual(self.request('draft-test/check', method='POST', nonce=True,
            body={'expected_version': 3})[0], 409)
        self.server.thunder_new_journey = None
        self.assertEqual(self.request('catalog')[0], 503)


class NewJourneyBoundHTTPTests(unittest.TestCase):
    request = NewJourneyHTTPTests.request
    stop = NewJourneyHTTPTests.stop

    def setUp(self):
        NewJourneyHTTPTests.setUp(self)
        from test_campaign_sheet_support import make_sheet
        from test_thunder_os_campaign_registry import config
        from thunder_os.db import Repository
        from thunder_os.new_journey import CHECK_KEYS, NewJourney
        from thunder_os.service import ThunderService
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        store, sheet = make_sheet(root / 'brain')
        self.repo = Repository(root / 'state.db')
        self.service = ThunderService(self.repo, knowledge_store=store)
        cfg = {**config(), 'campaign_id': 'BR-APOLLO-FIXTURE', 'platform': 'apollo',
               'providers': {'email': 'apollo'}, 'briefing': sheet,
               'configuration': {'flow_version': '1', 'provider_accounts': {'email': 'sender@example.invalid'}}}
        self.service.campaign_registry.register(cfg, 'ygor', 0)
        apollo = SimpleNamespace(sequences_search=lambda **_: {
            'status': 'ok', 'complete': True, 'credit_cost': 0,
            'source': {'provider': 'apollo', 'account_id': 'team-fixture',
                       'identity_source': 'provider_response_team'},
            'items': [{'id': 'sequence-fixture', 'name': 'Fixture sequence',
                       'active': True, 'archived': False, 'user_id': 'owner-fixture'}]})
        checker = lambda rows, campaign_id: passing(rows, campaign_id, account_id='team-fixture')
        self.server.thunder_service = self.service
        templates = StaticTemplates([{'channel': 'email', 'subject': 'Fixture subject',
            'content': 'Original exact text\nSecond paragraph', 'order': 1}])
        self.server.thunder_new_journey = NewJourney(self.service, apollo=apollo, checker=checker,
                                                    template_reader=templates)

    def test_N06_N07_real_service_http_persistence_exact_approval_and_csv(self):
        status, draft = self.request('create', method='POST', nonce=True,
            body={'campaign_id': 'BR-APOLLO-FIXTURE', 'name': 'Isolated HTTP draft'})
        self.assertEqual(status, 200)
        ident = draft['id']

        def post(action, **body):
            return self.request(ident + '/' + action, method='POST', nonce=True,
                                body={'expected_version': draft['version'], **body})

        status, draft = post('source', mode='csv', csv_text='name,company,email\nAna,Fixture,ana@example.invalid\n')
        self.assertEqual(status, 200)
        status, draft = post('check')
        self.assertEqual(status, 200)
        self.assertEqual(draft['counts']['eligible'], 1)
        step = {'id': 'E1', 'channel': 'email', 'subject': 'Fixture subject',
                'content': 'Original exact text\nSecond paragraph', 'order': 1, 'interval_hours': 0}
        status, draft = post('sequence', steps=[step], apollo_sequence_id='sequence-fixture',
                             apollo_sequence_query='Fixture sequence')
        self.assertEqual(status, 200)
        self.assertTrue(draft['steps'][0]['can_approve'])
        self.assertFalse(draft['can_export'])
        status, draft = post('approve-step', step_id='E1', review_hash=draft['review_hash'])
        self.assertEqual(status, 200)
        self.assertTrue(draft['can_export'])
        self.assertEqual(self.request(ident)[1]['review_hash'], draft['review_hash'])
        status, exported = post('export', review_hash=draft['review_hash'])
        self.assertEqual(status, 200)
        self.assertEqual(exported['rows'], 1)
        self.assertFalse(exported['enrollment_enabled'])
        self.assertIn('Original exact text\nSecond paragraph', exported['csv_text'])
        draft = self.request(ident)[1]
        status, revised = post('sequence', steps=[{**step, 'content': 'Changed text'}],
            apollo_sequence_id='sequence-fixture', apollo_sequence_query='Fixture sequence')
        self.assertEqual(status, 200)
        self.assertFalse(revised['can_export'])
        self.assertEqual(self.request(ident + '/export', method='POST', nonce=True,
            body={'expected_version': draft['version'], 'review_hash': draft['review_hash']})[0], 409)
        self.assertEqual(self.repo.effects(), [])
        self.assertEqual(self.repo.outbox(), [])
        self.assertEqual(self.repo.list_state('cadence'), [])


if __name__ == '__main__':
    unittest.main()
