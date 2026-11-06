"""Authenticated identity evidence with two fake HTTP transports, no network."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from thunder_os.apollo_linkedin_identity_binding import KIND, binding_key, create_identity_binding
from thunder_os.apollo_linkedin_authority import CampaignApprovalAuthority
from thunder_os.apollo_linkedin_reader import ApolloLinkedInReader
from thunder_os.connectsafely_client import ConnectSafelyClient
from thunder_os.connectsafely_read_targets import ReadTargetResolver, TargetReadHTTPS
from thunder_os.db import Repository
from thunder_os.service import ThunderService
from test_thunder_os_apollo_linkedin_authority import config

ACCOUNT = 'a' * 24
URN = 'urn:li:fsd_profile:synthetic'


class Response:
    status = 200
    def __init__(self, data): self.data = json.dumps(data).encode()
    def read(self, _size): return self.data


class FakeHTTPS:
    cached = False
    profile_account = ACCOUNT
    profile_urn = URN
    task_contact = 'contact'
    calls = []
    def __init__(self, host, port, **kwargs): self.host, self.path = host, None
    def request(self, method, path, **kwargs):
        if method != 'GET': raise AssertionError('unexpected mutation')
        self.path = path
        self.calls.append((self.host, method, path.split('?')[0]))
    def getresponse(self):
        if self.host == 'api.apollo.io':
            if self.path == '/api/v1/users/api_profile':
                return Response({'user': {'id': 'actor', 'team_id': 'team'}})
            if self.path == '/api/v1/tasks/task':
                return Response({'task': {'id': 'task', 'type': 'linkedin_step_message',
                    'status': 'scheduled', 'emailer_campaign_id': 'seq', 'user_id': 'owner',
                    'contact_id': self.task_contact, 'contact': {'id': self.task_contact,
                    'linkedin_url': 'https://www.linkedin.com/in/synthetic-person'}}})
        if self.host == 'api.connectsafely.ai':
            if self.path.startswith('/linkedin/accounts?'):
                return Response([{'id': ACCOUNT, 'publicId': 'sender'}])
            if self.path.startswith('/linkedin/profile?'):
                assert 'forceRefresh=true' in self.path, 'identity read must bypass provider cache'
                body = {'success': True, 'accountId': self.profile_account, 'profileId': 'synthetic-person',
                    'cached': self.cached, 'profile': {'publicIdentifier': 'synthetic-person', 'entityUrn': self.profile_urn}}
                if self.cached is None:
                    body.pop('cached')
                return Response(body)
        raise AssertionError('unexpected route')
    def close(self): pass


class IdentityBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'official.sqlite'; self.db.touch()
        self.env = patch.dict('os.environ', {'THUNDER_APOLLO_LI_OFFICIAL_DB': str(self.db)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.repo = Repository(self.db)
        service = ThunderService(self.repo)
        self.service = service
        service.configure_campaign(config(), 'ygor', expected_version=0)
        self.repo.put_state('control', 'global', {'paused': False, 'actor': 'ygor'})
        review = service.apollo_campaign_approval_review('campaign')
        self.approval = service.approve_apollo_campaign_version('campaign', 'ygor',
            execution_confirmation=review['execution_confirmation'])['id']
        self.http = patch('http.client.HTTPSConnection', FakeHTTPS)
        self.http.start(); self.addCleanup(self.http.stop)
        self.old_cached = FakeHTTPS.cached; FakeHTTPS.cached = False
        self.addCleanup(setattr, FakeHTTPS, 'cached', self.old_cached)
        self.addCleanup(setattr, FakeHTTPS, 'profile_account', FakeHTTPS.profile_account)
        self.addCleanup(setattr, FakeHTTPS, 'profile_urn', FakeHTTPS.profile_urn)
        FakeHTTPS.calls = []
        self.reader = ApolloLinkedInReader(lambda: 'synthetic-token', team_id='team',
                                           api_actor_id='actor', owner_ids=['owner'])
        transport = TargetReadHTTPS(ACCOUNT, lambda: 'synthetic-token-1234')
        client = ConnectSafelyClient(ACCOUNT, 'sender', request=transport)
        self.resolver = ReadTargetResolver(self.repo, client)

    def create(self):
        return create_identity_binding(self.repo, reader=self.reader, resolver=self.resolver,
            campaign_id='campaign', approval_id=self.approval, campaign_version='v1', task_id='task')

    def test_exact_identity_is_immutable_and_duplicate_is_idempotent(self):
        row = self.create(); self.assertEqual(row['contact_id'], 'contact')
        self.assertEqual(row['action_type'], 'linkedin_step_message')
        self.assertEqual(row['member_urn'], URN)
        self.assertEqual(row['id'], binding_key('company', 'campaign', 'task', ACCOUNT, self.approval))
        self.assertEqual(self.create()['version'], 1)
        self.assertEqual(len(self.repo.list_state(KIND)), 1)
        self.assertTrue(all(method == 'GET' for _, method, _ in FakeHTTPS.calls))

    def test_cached_profile_and_scope_mismatch_do_not_write_binding(self):
        FakeHTTPS.cached = True
        with self.assertRaisesRegex(ValueError, 'profile_identity_unverified'): self.create()
        self.assertEqual(self.repo.list_state(KIND), [])
        FakeHTTPS.cached = False
        self.reader.team_id = 'other'
        with self.assertRaisesRegex(ValueError, 'identity_authority_mismatch'): self.create()
        self.assertEqual(self.repo.list_state(KIND), [])

    def test_forced_read_without_cache_telemetry_binds(self):
        # Real provider shape (06/11/2026): forceRefresh=true omits `cached`.
        FakeHTTPS.cached = None
        self.assertEqual(self.create()['member_urn'], FakeHTTPS.profile_urn)

    def test_preexisting_other_identity_cannot_be_overwritten(self):
        result = self.create()
        raw = {k: v for k, v in result.items() if k not in ('id', 'version', 'updated_at')}
        raw['member_urn'] = 'urn:li:fsd_profile:other'
        self.repo.put_state(KIND, result['id'], raw)
        with self.assertRaisesRegex(ValueError, 'identity_binding_conflict'): self.create()
        self.assertEqual(self.repo.get_state(KIND, result['id'])['member_urn'], raw['member_urn'])

    def test_wrong_profile_account_or_malformed_urn_never_persists_identity(self):
        for key, value in [('profile_account', 'b' * 24), ('profile_urn', 'urn:li:other:invalid')]:
            with self.subTest(field=key), patch.object(FakeHTTPS, key, value):
                with self.assertRaises(ValueError): self.create()
                self.assertEqual(self.repo.list_state(KIND), [])

    def test_reapproval_appends_new_binding_without_changing_history(self):
        original = self.create()
        review = self.service.apollo_campaign_approval_review('campaign')
        self.approval = self.service.approve_apollo_campaign_version('campaign', 'ygor',
            execution_confirmation=review['execution_confirmation'])['id']
        renewed = self.create()
        self.assertNotEqual(original['id'], renewed['id'])
        self.assertEqual(self.repo.get_state(KIND, original['id']), original)
        self.assertEqual(renewed['approval_id'], self.approval)
        self.assertEqual(renewed['member_urn'], original['member_urn'])
        self.assertEqual(len(self.repo.list_state(KIND)), 2)

    def test_reader_transport_cannot_be_supplied_by_caller(self):
        self.reader._request = lambda *args: (200, b'{}')
        with self.assertRaisesRegex(ValueError, 'identity_authority_unverified'): self.create()
        self.assertEqual(self.repo.list_state(KIND), [])

    def test_task_contact_changes_during_resolution_never_persists_identity(self):
        get = self.reader.get
        calls = []
        def changed(task_id):
            result = get(task_id)
            calls.append(task_id)
            if len(calls) == 2:
                result['contact_id'] = result['contact']['id'] = 'other'
            return result
        with patch.object(self.reader, 'get', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'task_identity_changed'): self.create()
        self.assertEqual(self.repo.list_state(KIND), [])

    def test_real_sequence_is_persisted_and_changes_between_authorized_sequences_rejected(self):
        approved = CampaignApprovalAuthority(self.repo).require('campaign', self.approval, 'v1')
        approved['binding'] = {**approved['binding'], 'sequence_ids': ['seq', 'seq-two']}
        approved['binding'].pop('sequence_id')
        original_get = self.reader.get
        def get_second(task_id):
            task = original_get(task_id)
            task['emailer_campaign_id'] = 'seq-two'
            return task
        with patch.object(CampaignApprovalAuthority, 'require', return_value=approved):
            with patch.object(self.reader, 'get', side_effect=get_second):
                self.assertEqual(self.create()['sequence_id'], 'seq-two')

    def test_task_cannot_change_between_two_allowed_sequences_during_resolution(self):
        approved = CampaignApprovalAuthority(self.repo).require('campaign', self.approval, 'v1')
        approved['binding'] = {**approved['binding'], 'sequence_ids': ['seq', 'seq-two']}
        approved['binding'].pop('sequence_id')
        original_get = self.reader.get
        calls = []
        def changing(task_id):
            task = original_get(task_id)
            calls.append(task_id)
            if len(calls) == 2:
                task['emailer_campaign_id'] = 'seq-two'
            return task
        with patch.object(CampaignApprovalAuthority, 'require', return_value=approved):
            with patch.object(self.reader, 'get', side_effect=changing):
                with self.assertRaisesRegex(ValueError, 'task_identity_changed'):
                    self.create()
        self.assertEqual(self.repo.list_state(KIND), [])

    def test_sequence_outside_approval_cannot_create_identity(self):
        approved = CampaignApprovalAuthority(self.repo).require('campaign', self.approval, 'v1')
        approved['binding'] = {**approved['binding'], 'sequence_ids': ['seq-two', 'seq-three']}
        approved['binding'].pop('sequence_id')
        with patch.object(CampaignApprovalAuthority, 'require', return_value=approved):
            with self.assertRaisesRegex(ValueError, 'task_identity_unverified'):
                self.create()
        self.assertEqual(self.repo.list_state(KIND), [])


if __name__ == '__main__': unittest.main()
