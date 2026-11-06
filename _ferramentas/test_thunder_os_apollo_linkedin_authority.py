"""Existing approval-pack validation; synthetic records, no providers."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from thunder_os.apollo_linkedin_authority import CampaignApprovalAuthority
from thunder_os.approvals import ApprovalError, _step_hash, confirm_execution, execution_request
from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.db import Repository


def config():
    return {'campaign_id': 'campaign', 'campaign_version': 'v1', 'rules_version': 'r1',
            'run_id': 'run', 'platform': 'apollo', 'providers': {'linkedin': 'connectsafely'},
            'rules': 'synthetic approved rules', 'configuration': {
                'flow_version': '1', 'provider_accounts': {'linkedin': 'a' * 24},
                'linkedin_policy': {'timezone': 'UTC', 'weekly_invite_limit': 200, 'holidays': [], 'send_hour': 9},
                'apollo_linkedin': {'company_id': 'company', 'team_id': 'team', 'sequence_id': 'seq',
                    'owner_ids': ['owner'], 'account_id': 'a' * 24, 'public_id': 'sender'}}}


class CampaignApprovalAuthorityTest(unittest.TestCase):
    def setUp(self):
        import sys, types
        from unittest.mock import patch
        fake_toques = types.ModuleType("toques")
        fake_toques.ErroBanco = type("ErroBanco", (Exception,), {})
        with patch.dict(sys.modules, {"toques": fake_toques}):
            from thunder_os.service import ThunderService
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Repository(Path(self.tmp.name) / 'fake.db')
        self.registry = CampaignRegistry(self.repo)
        self.cfg = config(); self.registry.register(self.cfg, 'ygor', 0)
        self.repo.put_state('control', 'global', {'paused': False, 'actor': 'ygor'})
        self.service = ThunderService(self.repo)
        self.gate = CampaignApprovalAuthority(self.repo)

    def approve(self):
        review = self.service.apollo_campaign_approval_review('campaign')
        return self.service.approve_apollo_campaign_version('campaign', 'ygor', execution_confirmation=review['execution_confirmation'])

    def test_missing_approval_denied(self):
        with self.assertRaises(ApprovalError): self.gate.require('campaign', 'missing', 'v1')

    def test_real_emitter_approval_covers_binding(self):
        approval = self.approve()
        result = self.gate.require('campaign', approval['id'], 'v1')
        self.assertEqual(result['binding']['sequence_id'], 'seq')
        self.assertEqual(result['configuration']['linkedin_policy']['weekly_invite_limit'], 200)

    def test_required_custom_field_is_in_the_approved_snapshot(self):
        updated = deepcopy(self.cfg)
        updated['configuration']['required_custom_field_id'] = 'abm_field'
        self.registry.register(updated, 'ygor', 1)
        approval = self.approve()
        result = self.gate.require('campaign', approval['id'], 'v1')
        self.assertEqual(result['configuration']['required_custom_field_id'], 'abm_field')
        changed = deepcopy(updated)
        changed['configuration']['required_custom_field_id'] = 'another_field'
        self.registry.register(changed, 'ygor', 2)
        with self.assertRaises(ApprovalError):
            self.gate.require('campaign', approval['id'], 'v1')

    def test_two_sequences_and_action_fields_are_in_exact_approved_snapshot(self):
        updated = deepcopy(self.cfg)
        binding = updated['configuration']['apollo_linkedin']
        del binding['sequence_id']
        binding['sequence_ids'] = ['seq-one', 'seq-two']
        updated['configuration']['required_custom_field_ids'] = {
            'linkedin_step_connect': 'invite-field', 'linkedin_step_message': 'message-field'}
        self.registry.register(updated, 'ygor', 1)
        approval = self.approve()
        result = self.gate.require('campaign', approval['id'], 'v1')
        self.assertEqual(result['binding']['sequence_ids'], ['seq-one', 'seq-two'])
        self.assertEqual(result['configuration']['required_custom_field_ids']['linkedin_step_connect'], 'invite-field')
        changed = deepcopy(updated)
        changed['configuration']['apollo_linkedin']['sequence_ids'].append('seq-other')
        self.registry.register(changed, 'ygor', 2)
        with self.assertRaises(ApprovalError):
            self.gate.require('campaign', approval['id'], 'v1')

    def test_snapshot_mutation_denied(self):
        approval = self.approve()
        snapshot = self.repo.get_state('campaign_version_snapshot', approval['snapshot_id'])
        snapshot['scope']['campaign_config']['rules'] = 'tampered'
        self.repo.put_state('campaign_version_snapshot', approval['snapshot_id'], snapshot)
        with self.assertRaises(ApprovalError): self.gate.require('campaign', approval['id'], 'v1')

    def test_approval_mutation_denied(self):
        approval = self.approve(); approval['status'] = 'revoked'
        self.repo.put_state('approval', approval['id'], approval)
        with self.assertRaises(ApprovalError): self.gate.require('campaign', approval['id'], 'v1')

    def test_expired_confirmation_denied(self):
        from unittest.mock import patch
        from datetime import datetime, timedelta, timezone
        approval = self.approve()
        class Future(datetime):
            @classmethod
            def now(cls, tz=None): return datetime.now(timezone.utc) + timedelta(days=15)
        with patch('thunder_os.apollo_linkedin_authority.datetime', Future):
            with self.assertRaises(ApprovalError): self.gate.require('campaign', approval['id'], 'v1')

    def test_changed_version_and_historical_without_enrollment_denied(self):
        approval = self.approve()
        newer = deepcopy(self.cfg); newer['campaign_version'] = 'v2'
        newer['configuration']['apollo_linkedin']['sequence_id'] = 'different'
        self.registry.register(newer, 'ygor', 1)
        for version in ('v1', 'v2'):
            with self.assertRaises(ApprovalError): self.gate.require('campaign', approval['id'], version)

    def test_pause_or_control_change_denied(self):
        approval = self.approve()
        self.repo.put_state('control', 'global', {'paused': True, 'actor': 'ygor'})
        with self.assertRaises(ApprovalError): self.gate.require('campaign', approval['id'], 'v1')
        self.repo.put_state('control', 'global', {'paused': False, 'actor': 'ygor'})
        with self.assertRaises(ApprovalError): self.gate.require('campaign', approval['id'], 'v1')

    def test_legacy_approval_does_not_authorize(self):
        self.repo.put_state('approval', 'legacy', {'actor': 'ygor', 'status': 'active'})
        with self.assertRaises(ApprovalError): self.gate.require('campaign', 'legacy', 'v1')


if __name__ == '__main__': unittest.main()
