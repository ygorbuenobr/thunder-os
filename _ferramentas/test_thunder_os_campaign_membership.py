"""Task 3: synthetic HubSpot, isolated Repository, no network or credentials."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from thunder_os.campaign_membership import CampaignMembership, CampaignMembershipError
from thunder_os.campaign_membership_hubspot import CampaignMembershipHubSpot
from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.db import Repository
from thunder_os.integration_hubspot import PORTAL_ID
from thunder_os.hubspot_executor import HubSpotHTTPError


def config(ident, platform):
    return {'campaign_id': ident, 'campaign_version': '1', 'rules_version': '1',
            'run_id': 'synthetic', 'platform': platform,
            'providers': {'email': 'fake-email'}, 'rules': 'Synthetic only.',
            'configuration': {'flow_version': '1',
                              'provider_accounts': {'email': 'synthetic.invalid'}}}


class FakeHubSpot:
    """Simulates both properties and note readback; no real atomic API implied."""

    def __init__(self):
        self.portal_id = PORTAL_ID
        self.schema = {name: {'type': 'string', 'readOnlyValue': False}
                       for name in ('campanha_ativa', 'executor')}
        self.record = {'id': '100', 'archived': False, 'updatedAt': 'revision-1',
                       'properties': {'campanha_ativa': None, 'executor': None}}
        self.reads = 0
        self.writes = []
        self.notes = {}
        self.fault = None

    def portal(self):
        return self.portal_id

    def metadata(self, kind):
        assert kind == 'contacts'
        return {'portal_id': self.portal_id, 'object_type': kind,
                'properties': deepcopy(self.schema)}

    def read(self, kind, identifier, properties):
        assert kind == 'contacts' and identifier == '100'
        assert set(properties) == {'campanha_ativa', 'executor'}
        self.reads += 1
        return deepcopy(self.record)

    def write_membership(self, command):
        self.writes.append(deepcopy(command))
        if self.fault == 'timeout':
            raise TimeoutError('SECRET provider response')
        if self.fault == 'accepted_only':
            return {'contact_id': '100', 'note_id': '501'}
        self.record['properties'] = deepcopy(command['desired'])
        self.record['updatedAt'] = 'revision-' + str(len(self.writes) + 1)
        if command['note'] is not None:
            self.notes['501'] = {'id': '501', 'portal_id': PORTAL_ID,
                                 'contact_id': '100', 'body': command['note']}
        return {'contact_id': '100', 'note_id': '501' if command['note'] else None}

    def read_note(self, ident):
        if self.fault == 'missing_note':
            return None
        row = deepcopy(self.notes.get(ident))
        if row and self.fault == 'wrong_note_contact':
            row['contact_id'] = '101'
        return row


class FakeHTTP(FakeHubSpot):
    """Real adapter wire protocol over synthetic HTTP responses only."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.labels = [{'category': 'HUBSPOT_DEFINED', 'label': None, 'typeId': 998}]

    def request(self, method, path, body, headers, timeout):
        self.calls.append((method, path, deepcopy(body)))
        assert headers['Authorization'] == 'Bearer synthetic-token'
        assert 0 < timeout <= 30
        if path == '/account-info/v3/details':
            result = {'portalId': int(self.portal_id)}
        elif path == '/crm/v3/properties/contacts':
            result = {'results': [{'name': name, 'type': value['type'],
                'modificationMetadata': {'readOnlyValue': value['readOnlyValue']}}
                for name, value in self.schema.items()]}
        elif method == 'GET' and path.startswith('/crm/v3/objects/contacts/100?'):
            assert parse_qs(urlsplit(path).query) == {'properties': ['campanha_ativa,executor']}
            result = self.record
        elif method == 'PATCH' and path == '/crm/v3/objects/contacts/100':
            self.writes.append(deepcopy(body))
            if self.fault == 'http_403':
                return 403, b'{"message":"SECRET insufficient permission"}'
            if self.fault == 'http_timeout':
                raise TimeoutError('SECRET timeout')
            if self.fault != 'accepted_only':
                self.record['properties'] = deepcopy(body['properties'])
                self.record['updatedAt'] = 'revision-2'
            result = self.record
        elif path == '/crm/v4/associations/notes/contacts/labels':
            result = {'results': self.labels}
        elif method == 'POST' and path == '/crm/v3/objects/notes':
            self.notes['501'] = deepcopy(body)
            result = {'id': '501'}
        elif method == 'GET' and path == '/crm/v3/objects/notes/501?properties=hs_note_body&associations=contacts':
            result = {'id': '501', 'archived': False,
                'properties': {'hs_note_body': self.notes['501']['properties']['hs_note_body']},
                'associations': {'contacts': {'results': [{'id': '100', 'type': 'note_to_contact'}]}}}
            if self.fault == 'missing_note':
                result['properties'] = {}
            if self.fault == 'wrong_note_contact':
                result['associations']['contacts']['results'][0]['id'] = '101'
            if self.fault == 'paged_note':
                result['associations']['contacts']['paging'] = {'next': {'after': '2'}}
        else:
            raise AssertionError('unexpected HTTP request: ' + method + ' ' + path)
        return 200, json.dumps(result).encode()


class CampaignMembershipTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / '.scratch' / 'task3-membership'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name) / 'offline.db')
        registry = CampaignRegistry(self.repo)
        registry.register(config('BR-APOLLO-01', 'apollo'), 'ygor', 0)
        registry.register(config('BR-VAGAS-01', 'thunder_os'), 'ygor', 0)
        self.hubspot = FakeHubSpot()
        self.service = CampaignMembership(self.repo, self.hubspot)
        for target in ('socket.create_connection', 'http.client.HTTPSConnection',
                       'urllib.request.urlopen'):
            guard = patch(target, side_effect=AssertionError('network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def enabled(self, authorize=lambda command: True):
        return CampaignMembership(self.repo, self.hubspot, writer=self.hubspot,
                                  mutation_enabled=True, authorize=authorize)

    def occupied(self, campaign='BR-APOLLO-01', executor='apollo'):
        self.hubspot.record['properties'] = {'campanha_ativa': campaign,
                                            'executor': executor}

    def test_outer_transaction_cannot_hide_attempt_before_crm_write(self):
        row = self.service.propose('100', 'BR-APOLLO-01')
        before = deepcopy(row)
        with self.repo.transaction():
            with self.assertRaisesRegex(CampaignMembershipError, 'membership_outer_transaction_blocked'):
                self.enabled().execute('100', expected_version=row['version'])
        self.assertEqual(self.hubspot.writes, [])
        self.assertEqual(self.repo.get_state('campaign_membership_proposal', '100'), before)

    def test_lead_ocupado_bloqueia(self):
        self.occupied()
        row = self.service.propose('100', 'BR-VAGAS-01')
        self.assertEqual(row['status'], 'blocked')
        self.assertEqual(row['reason'], 'lead_occupied')
        self.assertEqual(row['contact_conflicts'], [{
            'contact_id': '100', 'campaign': 'BR-APOLLO-01',
            'requested_campaign': 'BR-VAGAS-01', 'executor': 'apollo',
            'platform': 'thunder_os', 'reason': 'lead_occupied'}])
        with self.assertRaisesRegex(CampaignMembershipError, 'proposal_not_ready'):
            self.enabled().execute('100', expected_version=row['version'])
        self.assertEqual(self.hubspot.record['properties']['campanha_ativa'], 'BR-APOLLO-01')
        self.assertEqual(self.hubspot.writes, [])

    def test_resposta_libera(self):
        self.occupied()
        row = self.service.propose('100', 'BR-APOLLO-01', action='release',
                                   reply_ref='synthetic-reply-1')
        self.assertEqual(row['status'], 'ready')
        result = self.enabled().execute('100', expected_version=row['version'])
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(self.hubspot.record['properties'], {'campanha_ativa': '', 'executor': ''})
        note = self.hubspot.notes['501']
        self.assertEqual(note['contact_id'], '100')
        self.assertIn('respondeu', note['body'])
        self.assertIn('BR-APOLLO-01', note['body'])
        self.assertIn('synthetic-reply-1', note['body'])
        self.assertEqual(result['note_id'], '501')
        self.assertTrue(result['readback_verified'])
        self.assertFalse(result['external_platforms_stopped'])
        free = self.service.inspect('100', 'BR-VAGAS-01')
        self.assertEqual(free['status'], 'ready')

    def test_default_off_blocks_assign_and_release(self):
        self.repo.put_state('control', 'global', {'external_effects_enabled': True})
        for action in ('assign', 'release'):
            with self.subTest(action=action):
                if action == 'release':
                    self.occupied()
                prior = self.repo.get_state('campaign_membership_proposal', '100')
                row = self.service.propose('100', 'BR-APOLLO-01', action=action,
                    reply_ref='reply-1' if action == 'release' else None,
                    expected_version=prior['version'] if prior else 0)
                default = CampaignMembership(self.repo, self.hubspot,
                    writer=self.hubspot, authorize=lambda command: True)
                reads = self.hubspot.reads
                with self.assertRaisesRegex(CampaignMembershipError, 'membership_mutations_disabled'):
                    default.execute('100', expected_version=row['version'])
                self.assertEqual(self.hubspot.reads, reads)
                self.assertFalse(default.inspect('100', 'BR-APOLLO-01')['mutation_enabled'])
                self.assertEqual(self.hubspot.writes, [])

    def test_draft_uses_repository_but_never_becomes_membership_truth(self):
        before = deepcopy(self.hubspot.record)
        row = self.service.propose('100', 'BR-APOLLO-01')
        self.assertEqual(row['status'], 'ready')
        self.assertEqual(row['desired'], {'campanha_ativa': 'BR-APOLLO-01', 'executor': 'apollo'})
        self.assertEqual(self.repo.get_state('campaign_membership_proposal', '100')['version'], 1)
        self.assertEqual(self.hubspot.record, before)
        self.occupied('BR-VAGAS-01', 'thunder_os')
        current = self.service.inspect('100', 'BR-APOLLO-01')
        self.assertEqual(current['membership']['campaign'], 'BR-VAGAS-01')
        self.assertEqual(current['source']['provider'], 'hubspot')
        self.assertEqual(current['status'], 'blocked')
        self.assertEqual(self.hubspot.writes, [])
        self.assertEqual(self.repo.effects(), [])
        self.assertEqual(self.repo.outbox(), [])

    def test_unknown_source_fails_closed(self):
        original_schema, original_record = deepcopy(self.hubspot.schema), deepcopy(self.hubspot.record)
        for fault in ('missing_schema', 'omitted', 'multiple', 'readonly', 'wrong_portal',
                      'wrong_id', 'archived', 'missing_contact', 'missing_revision', 'wrong_type'):
            with self.subTest(fault=fault):
                self.hubspot.schema = deepcopy(original_schema)
                self.hubspot.record = deepcopy(original_record)
                self.hubspot.portal_id = PORTAL_ID
                if fault == 'missing_schema': self.hubspot.schema.pop('executor')
                if fault == 'omitted': self.hubspot.record['properties'].pop('campanha_ativa')
                if fault == 'multiple': self.hubspot.record['properties']['campanha_ativa'] = 'A;B'
                if fault == 'readonly': self.hubspot.schema['executor']['readOnlyValue'] = True
                if fault == 'wrong_portal': self.hubspot.portal_id = '1'
                if fault == 'wrong_id': self.hubspot.record['id'] = '101'
                if fault == 'archived': self.hubspot.record['archived'] = True
                if fault == 'missing_contact': self.hubspot.record = None
                if fault == 'missing_revision': self.hubspot.record.pop('updatedAt')
                if fault == 'wrong_type': self.hubspot.schema['executor']['type'] = 'number'
                result = self.service.inspect('100', 'BR-APOLLO-01')
                self.assertEqual(result['status'], 'blocked')
                self.assertIsNone(result['desired'])
                self.assertNotEqual(result['source']['status'], 'verified')
                self.assertEqual(self.hubspot.writes, [])

    def test_executor_conflict_is_visible(self):
        for campaign, executor in (('BR-APOLLO-01', 'instantly'), (None, 'apollo'),
                                   ('BR-APOLLO-01', None)):
            with self.subTest(campaign=campaign, executor=executor):
                self.occupied(campaign, executor)
                result = self.service.inspect('100', 'BR-APOLLO-01')
                self.assertEqual(result['status'], 'blocked')
                self.assertTrue(result['contact_conflicts'])
                self.assertIn(result['reason'], ('executor_differs_from_registry_platform',
                                                'executor_without_campaign', 'executor_missing'))

    def test_stale_contact_or_registry_blocks_write(self):
        for fault in ('contact', 'revision', 'registry'):
            with self.subTest(fault=fault):
                repo = Repository(Path(self.temp.name) / (fault + '.db'))
                CampaignRegistry(repo).register(config('BR-APOLLO-01', 'apollo'), 'ygor', 0)
                remote = FakeHubSpot()
                service = CampaignMembership(repo, remote, writer=remote,
                    mutation_enabled=True, authorize=lambda command: True)
                row = service.propose('100', 'BR-APOLLO-01')
                if fault == 'contact': remote.record['properties']['campanha_ativa'] = 'OTHER'
                if fault == 'revision': remote.record['updatedAt'] = 'changed'
                if fault == 'registry':
                    CampaignRegistry(repo).register(config('BR-APOLLO-01', 'apollo'), 'ygor', 1)
                with self.assertRaisesRegex(CampaignMembershipError, 'membership_proposal_stale'):
                    service.execute('100', expected_version=row['version'])
                self.assertEqual(remote.writes, [])

    def test_competing_proposals_use_repository_cas(self):
        path = self.repo.path
        def propose(campaign):
            service = CampaignMembership(Repository(path), self.hubspot)
            try:
                return service.propose('100', campaign)['status']
            except CampaignMembershipError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(propose, ('BR-APOLLO-01', 'BR-VAGAS-01')))
        self.assertCountEqual(results, ['ready', 'membership_version_conflict'])
        self.assertEqual(self.repo.get_state('campaign_membership_proposal', '100')['version'], 1)
        self.assertEqual(self.hubspot.writes, [])

    def test_global_pause_and_authority(self):
        row = self.service.propose('100', 'BR-APOLLO-01')
        for authorize in (None, lambda command: False, lambda command: 'true'):
            with self.subTest(authorize=authorize), self.assertRaisesRegex(
                    CampaignMembershipError, 'membership_not_authorized'):
                self.enabled(authorize).execute('100', expected_version=row['version'])
        self.repo.put_state('control', 'global', {'paused': True})
        with self.assertRaisesRegex(CampaignMembershipError, 'global_paused'):
            self.enabled().execute('100', expected_version=row['version'])
        self.assertEqual(self.hubspot.writes, [])

    def test_uncertain_effect_never_retries(self):
        for fault in ('timeout', 'accepted_only', 'missing_note', 'wrong_note_contact'):
            with self.subTest(fault=fault):
                repo = Repository(Path(self.temp.name) / (fault + '.db'))
                CampaignRegistry(repo).register(config('BR-APOLLO-01', 'apollo'), 'ygor', 0)
                remote = FakeHubSpot()
                remote.record['properties'] = {'campanha_ativa': 'BR-APOLLO-01', 'executor': 'apollo'}
                remote.fault = fault
                service = CampaignMembership(repo, remote, writer=remote,
                    mutation_enabled=True, authorize=lambda command: True)
                row = service.propose('100', 'BR-APOLLO-01', action='release', reply_ref='reply-1')
                result = service.execute('100', expected_version=row['version'])
                self.assertEqual(result['status'], 'uncertain')
                self.assertFalse(result['readback_verified'])
                self.assertEqual(len(remote.writes), 1)
                replay = service.execute('100', expected_version=result['version'])
                self.assertEqual(replay['status'], 'uncertain')
                with self.assertRaisesRegex(CampaignMembershipError, 'membership_reconciliation_required'):
                    service.propose('100', 'BR-APOLLO-01', expected_version=result['version'])
                self.assertEqual(len(remote.writes), 1)
                self.assertNotIn('SECRET', str(result))

    def test_same_membership_and_confirmed_replay_are_noops(self):
        self.occupied()
        result = self.service.inspect('100', 'BR-APOLLO-01')
        self.assertEqual(result['status'], 'unchanged')
        self.hubspot.record = FakeHubSpot().record
        row = self.service.propose('100', 'BR-APOLLO-01')
        result = self.enabled().execute('100', expected_version=row['version'])
        self.assertEqual(result['status'], 'confirmed')
        replay = self.enabled().execute('100', expected_version=result['version'])
        self.assertEqual(replay['status'], 'confirmed')
        self.assertEqual(len(self.hubspot.writes), 1)

    def test_release_requires_matching_campaign_and_reply(self):
        self.occupied('BR-VAGAS-01', 'thunder_os')
        row = self.service.inspect('100', 'BR-APOLLO-01', action='release', reply_ref='reply-1')
        self.assertEqual(row['status'], 'blocked')
        self.assertEqual(row['reason'], 'release_campaign_mismatch')
        with self.assertRaisesRegex(CampaignMembershipError, 'invalid_reply_ref'):
            self.service.inspect('100', 'BR-APOLLO-01', action='release')
        self.assertEqual(self.hubspot.writes, [])

    def test_input_validation(self):
        reads = self.hubspot.reads
        for contact in ('0', 100, True, '../100', ' 100', '1' * 31, None):
            with self.subTest(contact=contact), self.assertRaises(CampaignMembershipError):
                self.service.inspect(contact, 'BR-APOLLO-01')
        for campaign in ('A;B', '', [], None, 'x' * 101):
            with self.subTest(campaign=campaign), self.assertRaises(CampaignMembershipError):
                self.service.inspect('100', campaign)
        for version in (True, -1, '1', None):
            with self.subTest(version=version), self.assertRaises(CampaignMembershipError):
                self.service.propose('100', 'BR-APOLLO-01', expected_version=version)
        with self.assertRaises(CampaignMembershipError):
            self.service.inspect('100', 'BR-APOLLO-01', action='mass_assign')
        self.assertEqual(self.hubspot.reads, reads)

    def test_transport_errors_are_sanitized(self):
        with patch.object(self.hubspot, 'read', side_effect=RuntimeError('SECRET credentials')):
            result = self.service.inspect('100', 'BR-APOLLO-01')
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'membership_source_unavailable')
        self.assertNotIn('SECRET', str(result))

    def http_service(self, remote, *, enabled=True, authorize=lambda command: True):
        port = CampaignMembershipHubSpot(credential_loader=lambda: 'synthetic-token',
            request=remote.request, mutation_enabled=enabled, authorize=authorize)
        return CampaignMembership(self.repo, port, writer=port,
            mutation_enabled=enabled, authorize=authorize), port

    def test_http_assign_only_patches_membership_properties(self):
        remote = FakeHTTP()
        service, port = self.http_service(remote)
        row = service.propose('100', 'BR-APOLLO-01')
        result = service.execute('100', expected_version=row['version'])
        self.assertEqual(result['status'], 'confirmed')
        mutations = [call for call in remote.calls if call[0] != 'GET']
        self.assertEqual(mutations, [('PATCH', '/crm/v3/objects/contacts/100', {
            'properties': {'campanha_ativa': 'BR-APOLLO-01', 'executor': 'apollo'}})])
        self.assertEqual(remote.calls[-1][0], 'GET')
        self.assertIn('/crm/v3/objects/contacts/100?', remote.calls[-1][1])

    def test_http_release_confirms_note_before_clearing_contact(self):
        remote = FakeHTTP()
        remote.record['properties'] = {'campanha_ativa': 'BR-APOLLO-01', 'executor': 'apollo'}
        service, port = self.http_service(remote)
        row = service.propose('100', 'BR-APOLLO-01', action='release', reply_ref='synthetic-reply')
        result = service.execute('100', expected_version=row['version'])
        self.assertEqual(result['status'], 'confirmed')
        mutations = [call for call in remote.calls if call[0] != 'GET']
        self.assertEqual([call[:2] for call in mutations], [
            ('POST', '/crm/v3/objects/notes'), ('PATCH', '/crm/v3/objects/contacts/100')])
        note_body = mutations[0][2]
        self.assertEqual(note_body['associations'], [{'to': {'id': '100'}, 'types': [{
            'associationCategory': 'HUBSPOT_DEFINED', 'associationTypeId': 998}]}])
        self.assertIn('synthetic-reply', note_body['properties']['hs_note_body'])
        self.assertTrue(note_body['properties']['hs_timestamp'].endswith('+00:00'))
        self.assertEqual(mutations[1][2], {'properties': {'campanha_ativa': '', 'executor': ''}})
        first_note_read = next(i for i, call in enumerate(remote.calls)
                               if call[0] == 'GET' and '/objects/notes/501?' in call[1])
        patch_index = next(i for i, call in enumerate(remote.calls) if call[0] == 'PATCH')
        self.assertLess(first_note_read, patch_index)

    def test_http_adapter_default_off_before_any_http(self):
        loader, request = Mock(side_effect=AssertionError('no credentials')), Mock()
        port = CampaignMembershipHubSpot(credential_loader=loader, request=request,
                                        authorize=lambda command: True)
        self.repo.put_state('control', 'global', {'external_effects_enabled': True})
        row = self.service.propose('100', 'BR-APOLLO-01')
        for command in (row, None, {}):
            with self.subTest(command=command), self.assertRaisesRegex(
                    CampaignMembershipError, 'membership_mutations_disabled'):
                port.write_membership(command)
        loader.assert_not_called()
        request.assert_not_called()
        enabled_core = CampaignMembership(self.repo, self.hubspot, writer=port,
            mutation_enabled=True, authorize=lambda command: True)
        result = enabled_core.execute('100', expected_version=row['version'])
        self.assertEqual(result['status'], 'uncertain')
        request.assert_not_called()

    def test_http_adapter_failure_boundaries(self):
        for fault in ('missing_note', 'wrong_note_contact', 'paged_note', 'ambiguous_labels',
                      'missing_labels', 'http_403', 'http_timeout', 'accepted_only'):
            with self.subTest(fault=fault):
                repo = Repository(Path(self.temp.name) / ('http-' + fault + '.db'))
                CampaignRegistry(repo).register(config('BR-APOLLO-01', 'apollo'), 'ygor', 0)
                remote = FakeHTTP()
                remote.record['properties'] = {'campanha_ativa': 'BR-APOLLO-01', 'executor': 'apollo'}
                remote.fault = fault
                if fault == 'ambiguous_labels': remote.labels *= 2
                if fault == 'missing_labels': remote.labels = []
                port = CampaignMembershipHubSpot(credential_loader=lambda: 'synthetic-token',
                    request=remote.request, mutation_enabled=True, authorize=lambda command: True)
                service = CampaignMembership(repo, port, writer=port, mutation_enabled=True,
                    authorize=lambda command: True)
                row = service.propose('100', 'BR-APOLLO-01', action='release', reply_ref='reply-1')
                result = service.execute('100', expected_version=row['version'])
                self.assertEqual(result['status'], 'uncertain')
                self.assertFalse(result['readback_verified'])
                self.assertNotIn('SECRET', str(result))
                if fault not in ('http_403', 'http_timeout', 'accepted_only'):
                    self.assertFalse(any(call[0] == 'PATCH' for call in remote.calls))
                calls = len(remote.calls)
                service.execute('100', expected_version=result['version'])
                self.assertEqual(len(remote.calls), calls)

    def test_http_fixed_transport_and_loader(self):
        from thunder_os.hubspot_console import systemd_hubspot_credential
        self.assertIs(CampaignMembershipHubSpot.__init__.__kwdefaults__['credential_loader'],
                      systemd_hubspot_credential)
        loader = Mock(return_value='synthetic-token')
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = json.dumps({'portalId': int(PORTAL_ID)}).encode()
        with patch('http.client.HTTPSConnection', return_value=connection) as https:
            port = CampaignMembershipHubSpot(credential_loader=loader)
            self.assertEqual(port.portal(), PORTAL_ID)
        self.assertEqual(https.call_args.args, ('api.hubapi.com', 443))
        self.assertEqual(https.call_args.kwargs['timeout'], 20)
        connection.request.assert_called_once()
        self.assertEqual(connection.request.call_args.args[:2], ('GET', '/account-info/v3/details'))
        connection.close.assert_called_once()
        loader.assert_called_once()

    def test_http_readonly_inspect_composition_and_adapter_authority(self):
        remote = FakeHTTP()
        service, port = self.http_service(remote, enabled=False, authorize=None)
        self.occupied()
        remote.record['properties'] = deepcopy(self.hubspot.record['properties'])
        row = service.inspect('100', 'BR-VAGAS-01')
        self.assertEqual(row['reason'], 'lead_occupied')
        self.assertFalse(row['mutation_enabled'])
        self.assertTrue(all(call[0] == 'GET' for call in remote.calls))
        port.mutation_enabled = True
        with self.assertRaisesRegex(CampaignMembershipError, 'membership_not_authorized'):
            port.write_membership(row)
        self.assertTrue(all(call[0] == 'GET' for call in remote.calls))

    def test_http_target_mismatch_blocks_every_mutation(self):
        command = self.service.propose('100', 'BR-APOLLO-01')
        for fault in ('portal', 'contact', 'revision', 'schema', 'missing_property'):
            with self.subTest(fault=fault):
                remote = FakeHTTP()
                if fault == 'portal': remote.portal_id = '1'
                if fault == 'contact': remote.record['id'] = '101'
                if fault == 'revision': remote.record['updatedAt'] = 'stale'
                if fault == 'schema': remote.schema.pop('executor')
                if fault == 'missing_property': remote.record['properties'].pop('executor')
                port = CampaignMembershipHubSpot(credential_loader=lambda: 'synthetic-token',
                    request=remote.request, mutation_enabled=True, authorize=lambda command: True)
                with self.assertRaises((CampaignMembershipError, HubSpotHTTPError)):
                    port.write_membership(command)
                self.assertTrue(all(call[0] == 'GET' for call in remote.calls))


if __name__ == '__main__':
    unittest.main()
