"""Campaign-authorized message path, with temporary state and fake HTTP only."""
import json
import os
from unittest.mock import patch

from thunder_os import apollo_linkedin_live_executor as runtime
from thunder_os import apollo_linkedin_reader as reader_module
from thunder_os.connectsafely_client import ConnectSafelyClient
from thunder_os.connectsafely_read_targets import ReadTargetResolver, TargetReadHTTPS
import test_thunder_os_apollo_linkedin_t4d_e2e as fixtures
from test_thunder_os_apollo_linkedin_identity_binding import FakeHTTPS, Response


class ResolverHTTPS(FakeHTTPS):
    def getresponse(self):
        if self.host == 'api.connectsafely.ai' and self.path.startswith('/linkedin/profile?'):
            return Response({'success': True, 'accountId': fixtures.ACCOUNT, 'profileId': 'fixture-person',
                'cached': False, 'profile': {'publicIdentifier': 'fixture-person',
                                             'entityUrn': fixtures.RECIPIENT}})
        return super().getresponse()


class CampaignRunnerTest(fixtures.T4dEndToEnd):
    def setUp(self):
        original_config = fixtures.config
        def personalized_config():
            cfg = original_config()
            cfg['configuration']['required_custom_field_id'] = 'abm_field'
            return cfg
        config_patch = patch.object(fixtures, 'config', side_effect=personalized_config)
        config_patch.start(); self.addCleanup(config_patch.stop)
        super().setUp()
        task = self.fake.task
        def personalized_task():
            value = task()
            value['contact']['typed_custom_fields'] = {'abm_field': fixtures.TEXT}
            return value
        self.fake.task = personalized_task
        connection = patch('http.client.HTTPSConnection', ResolverHTTPS)
        connection.start(); self.addCleanup(connection.stop)
        identity_transport = patch('thunder_os.apollo_linkedin_identity_binding._https_request',
                                   reader_module._https_request)
        identity_transport.start(); self.addCleanup(identity_transport.stop)
        transport = TargetReadHTTPS(fixtures.ACCOUNT, lambda: 'synthetic-token-1234')
        client = ConnectSafelyClient(fixtures.ACCOUNT, 'sender', request=transport)
        self.resolver = ReadTargetResolver(self.repo, client)

    def run_campaign(self):
        coordinator = runtime.build_campaign_runtime('campaign', self.approval['approval_id'], 'v1')
        coordinator.target_resolver = self.resolver
        if isinstance(self.resolver, ReadTargetResolver):
            self.resolver.repo = coordinator.repo
        return coordinator.run_campaign('fixture-task', 'campaign', self.approval['approval_id'], 'v1',
                                        dry_run=False)

    def test_approved_campaign_reserves_and_dispatches_without_consuming_canary(self):
        with self.repo.transaction() as con:
            con.execute("DELETE FROM thunder_state WHERE kind='apollo_linkedin_canary'")
        self.assertEqual(self.repo.list_state('apollo_linkedin_canary'), [])
        result = self.run_campaign()
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(len(self.posts()), 1)
        row = self.repo.get_apollo_linkedin_reservation('company', 'fixture-task', 'linkedin_step_message')
        self.assertEqual(row['state'], 'confirmed')
        self.assertEqual(row['apollo_status'], 'confirmed')
        self.assertEqual(self.repo.list_state('apollo_linkedin_canary'), [])
        identity = self.repo.list_state('apollo_linkedin_identity_binding')
        self.assertEqual(len(identity), 1)
        self.assertEqual(identity[0]['member_urn'], fixtures.RECIPIENT)
        self.assertEqual(identity[0]['contact_id'], 'contact')
        with self.repo._connect() as con:
            events = con.execute("SELECT payload FROM thunder_events WHERE type='apollo_linkedin_campaign_reservation'").fetchall()
        self.assertEqual(len(events), 1)
        evidence = json.loads(events[0]['payload'])
        self.assertEqual(evidence['binding_id'], identity[0]['id'])
        self.assertEqual(evidence['member_urn'], fixtures.RECIPIENT)
        self.assertNotEqual(self.run_campaign()['state'], 'confirmed')
        self.assertEqual(len(self.posts()), 1)

    def test_one_approval_accepts_two_sequences_and_rejects_third(self):
        from thunder_os.service import ThunderService
        for sequence, expected in [('seq', 'confirmed'), ('seq2', 'confirmed'), ('foreign', 'blocked')]:
            with self.subTest(sequence=sequence):
                case = type(self)(methodName='runTest')
                case.setUp()
                try:
                    service = ThunderService(case.repo)
                    cfg = fixtures.config()
                    binding = cfg['configuration']['apollo_linkedin']
                    binding.pop('sequence_id')
                    binding['sequence_ids'] = ['seq', 'seq2']
                    cfg['configuration'].pop('required_custom_field_id', None)
                    cfg['configuration']['required_custom_field_ids'] = {
                        'linkedin_step_connect': 'invite_field', 'linkedin_step_message': 'message_field'}
                    service.configure_campaign(cfg, 'ygor', expected_version=1)
                    review = service.apollo_campaign_approval_review('campaign')
                    approval = service.approve_apollo_campaign_version('campaign', 'ygor',
                        execution_confirmation=review['execution_confirmation'])
                    case.approval = {'approval_id': approval['id']}
                    task = case.fake.task
                    def scoped_task():
                        item = task()
                        item['emailer_campaign_id'] = sequence
                        item['contact']['typed_custom_fields'] = {'message_field': fixtures.TEXT}
                        return item
                    case.fake.task = scoped_task
                    def contact_request(method, path, body, headers, timeout):
                        status, payload = case.fake.apollo(method, path, body, headers, timeout)
                        if '/contacts/' in path:
                            value = json.loads(payload)
                            value['contact']['contact_campaign_statuses'][0]['emailer_campaign_id'] = sequence
                            payload = json.dumps(value).encode()
                        return status, payload
                    with patch('thunder_os.apollo_linkedin_reply_coverage._contact_request', contact_request):
                        result = case.run_campaign()
                    self.assertEqual(expected, result['state'], result)
                    self.assertEqual(1 if expected == 'confirmed' else 0, len(case.posts()))
                    if expected == 'confirmed':
                        identity = case.repo.list_state('apollo_linkedin_identity_binding')
                        self.assertEqual(sequence, identity[0]['sequence_id'])
                finally:
                    case.doCleanups()

    def test_automated_campaign_without_required_field_stops_before_provider(self):
        class Authority:
            def require(self, *args):
                return {'binding': {}, 'configuration': {}}
        coordinator = runtime.MessageCoordinator(repository=self.repo, authority=Authority(),
            apollo=None, message=None, coverage=None, safety=None, completion=None)
        result = coordinator.run_campaign('fixture-task', 'campaign', 'approval', 'v1', dry_run=False)
        self.assertEqual('required_custom_field_unconfigured', result['reason'])
        self.assertEqual([], self.posts())

    def test_campaign_binding_or_disabled_key_never_reserves_or_posts(self):
        self.resolver = lambda _: {'resolved': True, 'read_only': True,
            'source': 'provider_observation', 'target': {'account_id': 'wrong'}}
        self.assertEqual(self.run_campaign()['state'], 'blocked')
        self.assertEqual(self.repo.list_apollo_linkedin_reservations(), [])
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'false'}):
            self.assertEqual(self.run_campaign()['reason'], 'execution_disabled')
        self.assertEqual(self.posts(), [])

    def test_dotted_campaign_version_is_supported(self):
        from thunder_os.service import ThunderService
        from test_thunder_os_apollo_linkedin_authority import config
        service = ThunderService(self.repo)
        changed = config()
        changed['configuration']['required_custom_field_id'] = 'abm_field'
        changed['campaign_version'] = 'v1.0'
        service.configure_campaign(changed, 'ygor', expected_version=1)
        review = service.apollo_campaign_approval_review('campaign')
        approval = service.approve_apollo_campaign_version('campaign', 'ygor',
            execution_confirmation=review['execution_confirmation'])
        coordinator = runtime.build_campaign_runtime('campaign', approval['id'], 'v1.0')
        coordinator.target_resolver = self.resolver
        self.resolver.repo = coordinator.repo
        result = coordinator.run_campaign('fixture-task', 'campaign', approval['id'], 'v1.0', dry_run=False)
        self.assertEqual(result['state'], 'confirmed')

    def test_revoked_approval_never_reserves_or_sends(self):
        approval = self.repo.get_state('approval', self.approval['approval_id'])
        approval['status'] = 'revoked'
        self.repo.put_state('approval', self.approval['approval_id'], approval)
        from thunder_os.apollo_linkedin_authority import CampaignApprovalAuthority
        coordinator = runtime.MessageCoordinator(repository=self.repo,
            authority=CampaignApprovalAuthority(self.repo), apollo=None, message=None,
            coverage=None, safety=None, completion=None)
        coordinator.target_resolver = self.resolver
        result = coordinator.run_campaign('fixture-task', 'campaign', self.approval['approval_id'], 'v1', dry_run=False)
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(self.repo.list_apollo_linkedin_reservations(), [])
        self.assertEqual(self.posts(), [])

    def test_task_closed_during_identity_resolution_never_reserves_or_sends(self):
        observe = self.resolver._profile
        def close_before_return(profile_url):
            result = observe(profile_url)
            self.fake.status = 'completed'
            return result
        self.resolver._profile = close_before_return
        self.assertEqual(self.run_campaign()['state'], 'blocked')
        self.assertEqual(self.repo.list_apollo_linkedin_reservations(), [])
        self.assertEqual(self.posts(), [])

    def _assert_changed_after_reservation_blocks_send(self, field):
        coordinator = runtime.build_campaign_runtime('campaign', self.approval['approval_id'], 'v1')
        coordinator.target_resolver = self.resolver
        self.resolver.repo = coordinator.repo
        send = coordinator.message.send
        original_task = self.fake.task
        def modify_before_dispatch(*args, **kwargs):
            if field == 'approval':
                approval = self.repo.get_state('approval', self.approval['approval_id'])
                approval['status'] = 'revoked'
                self.repo.put_state('approval', self.approval['approval_id'], approval)
            else:
                def changed():
                    task = original_task()
                    if field == 'owner': task['user_id'] = 'other-owner'
                    if field == 'text': task['linkedin_emailer_template']['body_text'] = 'changed'
                    if field == 'contact':
                        task['contact_id'] = 'other-contact'
                        task['contact']['id'] = 'other-contact'
                    return task
                self.fake.task = changed
            return send(*args, **kwargs)
        coordinator.message.send = modify_before_dispatch
        result = coordinator.run_campaign('fixture-task', 'campaign', self.approval['approval_id'], 'v1', dry_run=False)
        self.assertNotEqual(result['state'], 'confirmed')
        self.assertEqual(self.posts(), [])

    def test_owner_changed_after_reservation_blocks_send(self):
        self._assert_changed_after_reservation_blocks_send('owner')

    def test_text_changed_after_reservation_blocks_send(self):
        self._assert_changed_after_reservation_blocks_send('text')

    def test_contact_changed_after_reservation_blocks_send(self):
        self._assert_changed_after_reservation_blocks_send('contact')

    def test_approval_revoked_after_reservation_blocks_send(self):
        self._assert_changed_after_reservation_blocks_send('approval')
