"""Offline registry contract tests: temporary SQLite, no external providers."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from unittest.mock import PropertyMock, patch
import tempfile
import unittest

from thunder_os.approvals import ApprovalError
from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.db import Repository
from thunder_os.models import payload_hash


def config():
    return {'campaign_id': 'synthetic', 'campaign_version': '1', 'rules_version': '1',
            'run_id': 'run-1', 'platform': 'thunder_os', 'providers': {'email': 'fake-email'},
            'rules': 'Regra sintética: somente contatos de teste.',
            'configuration': {'flow_version': '1', 'provider_accounts': {'email': 'synthetic-account'}}}


class CampaignRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name) / 'offline.db')
        self.registry = CampaignRegistry(self.repo)

    def test_digests_and_binding_are_derived_and_detached(self):
        cfg = config()
        row = self.registry.register(cfg, 'ygor', 0)
        ctx = self.registry.load('synthetic')
        self.assertEqual(row['version'], 1)
        self.assertEqual(ctx['rules_hash'], hashlib.sha256(cfg['rules'].encode()).hexdigest())
        self.assertEqual(ctx['configuration_hash'], payload_hash({
            'configuration': cfg['configuration'], 'providers': cfg['providers'], 'revision': 1}))
        self.assertEqual(self.registry.binding('synthetic'), {
            'providers': {'email': 'fake-email'}, 'provider_accounts': {'email': 'synthetic-account'}})
        cfg['providers']['email'] = 'mutated'; ctx['providers']['email'] = 'mutated'
        self.assertEqual(self.registry.load('synthetic')['providers']['email'], 'fake-email')

    def test_only_human_may_register_or_revoke(self):
        for actor in ('hermes', 'claude', '', None):
            with self.subTest(actor=actor), self.assertRaises(ApprovalError):
                self.registry.register(config(), actor, 0)
        self.assertIsNone(self.repo.get_state('campaign_config', 'synthetic'))
        self.registry.register(config(), 'ygor', 0)
        with self.assertRaises(ApprovalError): self.registry.revoke('synthetic', 'hermes', 1)
        self.assertTrue(self.repo.get_state('campaign_config', 'synthetic')['active'])

    def test_cas_register_and_revoke_preserve_current_state(self):
        self.registry.register(config(), 'ygor', 0)
        for version in (0, 2):
            with self.subTest(version=version):
                with self.assertRaisesRegex(ApprovalError, 'version_conflict'):
                    self.registry.register(config(), 'ygor', version)
                with self.assertRaisesRegex(ApprovalError, 'version_conflict'):
                    self.registry.revoke('synthetic', 'ygor', version)
        self.assertEqual(self.repo.get_state('campaign_config', 'synthetic')['version'], 1)
        for version in (True, -1, '1', None):
            with self.subTest(version=version), self.assertRaises(ApprovalError):
                self.registry.register(config(), 'ygor', version)

    def test_revoke_and_reregister_never_revive_same_hash(self):
        first = self.registry.register(config(), 'ygor', 0)['context']
        second = self.registry.register(config(), 'ygor', 1)['context']
        self.assertNotEqual(first['configuration_hash'], second['configuration_hash'])
        revoked = self.registry.revoke('synthetic', 'ygor', 2)
        self.assertFalse(revoked['active'])
        for read in (self.registry.load, self.registry.binding):
            with self.assertRaisesRegex(ApprovalError, 'revoked'): read('synthetic')
        third = self.registry.register(config(), 'ygor', 3)['context']
        self.assertNotIn(third['configuration_hash'], [first['configuration_hash'], second['configuration_hash']])
        self.assertEqual(self.registry.load('synthetic'), third)

    def test_strict_config_fields_and_types(self):
        cases = []
        for key in config():
            bad = config(); del bad[key]; cases.append(bad)
        for field, value in [('extra', True), ('platform', []), ('campaign_version', 1),
                             ('rules', ''), ('rules', ' '), ('rules', 'x'*65537),
                             ('providers', {'sms':'fake'}), ('providers', {'email': 'INVALID'}),
                             ('configuration', {'flow_version': '1', 'provider_accounts': {}, 'extra': True}),
                             ('configuration', {'flow_version': '', 'provider_accounts': {'email':'x'}}),
                             ('configuration', {'flow_version': '1', 'provider_accounts': {'email': 123}})]:
            bad = config(); bad[field] = value; cases.append(bad)
        for bad in cases:
            with self.subTest(config=repr(bad)[:180]), self.assertRaises(ApprovalError):
                self.registry.register(bad, 'ygor', 0)
        self.assertIsNone(self.repo.get_state('campaign_config', 'synthetic'))

    def test_persisted_tampering_is_detected(self):
        original = self.registry.register(config(), 'ygor', 0)
        cases = []
        for field, value in [('authorized_by','hermes'), ('active',1), ('unexpected',True)]:
            bad = deepcopy(original); bad[field] = value; cases.append(bad)
        bad=deepcopy(original);bad['config']['rules']='tampered';cases.append(bad)
        bad=deepcopy(original);bad['config']['campaign_id']='other';cases.append(bad)
        bad=deepcopy(original);bad['context']['rules_hash']='f'*64;cases.append(bad)
        bad=deepcopy(original);bad['config']['configuration']['provider_accounts']['email']='other';cases.append(bad)
        for bad in cases:
            with self.subTest(value=bad):
                payload={k:v for k,v in bad.items() if k not in {'id','version','updated_at'}}
                with self.repo.transaction() as con:
                    con.execute('UPDATE thunder_state SET payload=? WHERE kind=? AND entity_id=?',
                                (json.dumps(payload),'campaign_config','synthetic'))
                for read in (self.registry.load,self.registry.binding):
                    with self.assertRaises(ApprovalError):read('synthetic')

    def test_caller_transaction_sees_registration_and_rolls_back(self):
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.repo.transaction():
                self.registry.register(config(),'ygor',0)
                self.assertEqual(self.registry.load('synthetic')['campaign_id'],'synthetic')
                self.assertEqual(self.registry.binding('synthetic')['providers'],{'email':'fake-email'})
                raise RuntimeError('rollback')
        with self.assertRaisesRegex(ApprovalError,'missing'):self.registry.load('synthetic')

    def test_reentrant_dispatch_fence_blocks_writes_but_allows_reads(self):
        self.registry.register(config(), 'ygor', 0)
        with patch.object(Repository, 'dispatch_fence_active', new_callable=PropertyMock, create=True, return_value=True):
            for operation in (lambda: self.registry.register(config(), 'ygor', 1),
                              lambda: self.registry.revoke('synthetic', 'ygor', 1)):
                with self.assertRaisesRegex(ApprovalError, 'campaign_dispatch_in_progress'):operation()
            self.assertEqual(self.registry.load('synthetic')['campaign_id'], 'synthetic')
            self.assertEqual(self.registry.binding('synthetic')['providers'], {'email': 'fake-email'})
        self.assertEqual(self.repo.get_state('campaign_config', 'synthetic')['version'], 1)

    def test_malformed_persisted_json_is_typed_denial(self):
        self.registry.register(config(), 'ygor', 0)
        with self.repo.transaction() as con:
            con.execute("UPDATE thunder_state SET payload='not-json' WHERE kind='campaign_config'")
        with self.assertRaisesRegex(ApprovalError, 'source_invalid'):self.registry.load('synthetic')

    def test_missing_registry_never_returns_a_binding(self):
        for read in (self.registry.load,self.registry.binding):
            with self.assertRaisesRegex(ApprovalError,'missing'):read('unknown')


if __name__ == '__main__': unittest.main()
