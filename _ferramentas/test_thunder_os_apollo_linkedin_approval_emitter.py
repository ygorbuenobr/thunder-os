"""Campaign-scope approval uses synthetic local state and never sends."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from thunder_os.approvals import ApprovalError, ApprovalService
from thunder_os.db import Repository, RepositoryConflict
from thunder_os.service import AuthorizationError, ThunderService
from test_thunder_os_apollo_linkedin_authority import config


class ApolloCampaignApprovalEmitterTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.repo = Repository(Path(tmp.name) / 'fake.db')
        self.service = ThunderService(self.repo)
        self.cfg = config()
        self.service.configure_campaign(self.cfg, 'ygor', expected_version=0)
        self.repo.put_state('control', 'global', {'paused': False, 'actor': 'ygor'})

    def approve(self):
        review = self.service.apollo_campaign_approval_review('campaign')
        return review, self.service.approve_apollo_campaign_version(
            'campaign', 'ygor', execution_confirmation=review['execution_confirmation'])

    def test_approval_persists_snapshot_and_exact_confirmation(self):
        review, approval = self.approve()
        snapshot = self.repo.get_state('campaign_version_snapshot', approval['snapshot_id'])
        self.assertEqual(snapshot['version'], 1)
        self.assertEqual(snapshot['scope'], review['scope'])
        self.assertEqual(snapshot['scope']['campaign_config'], self.service.campaign_registry._read('campaign')[0]['config'])
        self.assertEqual(snapshot['scope_hash'], approval['scope_hash'])
        self.assertEqual(approval['scope'], 'campaign_version')
        self.assertEqual(approval['execution_confirmation']['request'], review['execution_confirmation'])
        with self.assertRaises(RepositoryConflict):
            self.repo.append_state('campaign_version_snapshot', approval['snapshot_id'], {})

    def test_exact_confirmation_and_human_required(self):
        review = self.service.apollo_campaign_approval_review('campaign')
        with self.assertRaises(AuthorizationError):
            self.service.approve_apollo_campaign_version('campaign', 'agent', execution_confirmation=review['execution_confirmation'])
        with self.assertRaises(ApprovalError):
            self.service.approve_apollo_campaign_version('campaign', 'ygor', execution_confirmation=True)
        self.assertEqual(self.repo.list_state('approval'), [])
        self.assertEqual(self.repo.list_state('campaign_version_snapshot'), [])

    def test_changed_version_cannot_reuse_confirmation_and_old_snapshot_remains(self):
        review, approval = self.approve()
        newer = deepcopy(self.cfg); newer['campaign_version'] = 'v2'
        newer['rules'] = 'revised synthetic rules'
        newer['configuration']['apollo_linkedin']['sequence_id'] = 'another'
        self.service.configure_campaign(newer, 'ygor', expected_version=1)
        with self.assertRaises(ApprovalError):
            self.service.approve_apollo_campaign_version('campaign', 'ygor', execution_confirmation=review['execution_confirmation'])
        current = self.service.apollo_campaign_approval_review('campaign')
        self.assertNotEqual(current['scope_hash'], approval['scope_hash'])
        self.assertEqual(self.repo.get_state('campaign_version_snapshot', approval['snapshot_id'])['scope'], review['scope'])
        self.assertEqual(review['scope']['campaign_config']['rules'], 'synthetic approved rules')

    def test_campaign_pack_never_authorizes_legacy_step(self):
        now = datetime.now(timezone.utc)
        service = ApprovalService(lambda: False)
        pack = service.approve_campaign_version('campaign', 1, 'a'*64, 'ygor', now + timedelta(hours=1), now)
        service.authorize_campaign_version(pack, 'a'*64, now)
        with self.assertRaisesRegex(ApprovalError, 'campaign_scope_does_not_authorize_step'):
            service.authorize_step({'id': 'step'}, pack, now)
        with self.assertRaises(ApprovalError):
            service.authorize_campaign_version(pack, 'b'*64, now)
        with self.assertRaises(ApprovalError):
            service.authorize_campaign_version(pack, 'a'*64, now + timedelta(hours=1))

    def test_global_pause_denies_review(self):
        self.repo.put_state('control', 'global', {'paused': True, 'actor': 'ygor'})
        with self.assertRaises(ApprovalError): self.service.apollo_campaign_approval_review('campaign')

    def test_mutated_snapshot_version_is_not_reused(self):
        review, approval = self.approve()
        snapshot = self.repo.get_state('campaign_version_snapshot', approval['snapshot_id'])
        self.repo.put_state('campaign_version_snapshot', approval['snapshot_id'],
                            {'scope': snapshot['scope'], 'scope_hash': snapshot['scope_hash']})
        with self.assertRaises(RepositoryConflict):
            self.service.approve_apollo_campaign_version('campaign', 'ygor', execution_confirmation=review['execution_confirmation'])


if __name__ == '__main__': unittest.main()
