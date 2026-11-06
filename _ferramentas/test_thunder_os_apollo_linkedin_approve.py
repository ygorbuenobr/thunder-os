"""Offline tests for the root-only campaign approval issuer."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from thunder_os.apollo_linkedin_approve import approve_campaign, main, read_config
from thunder_os.apollo_linkedin_authority import CampaignApprovalAuthority
from thunder_os.db import Repository
from test_thunder_os_apollo_linkedin_authority import config


class ApprovalCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'official.sqlite'
        self.db.touch()
        self.env = patch.dict(os.environ, {'PAINEL_DB': str(self.db),
            'THUNDER_APOLLO_LI_OFFICIAL_DB': str(self.db)}, clear=False)
        self.env.start(); self.addCleanup(self.env.stop)
        self.repo = Repository(str(self.db))
        self.repo.put_state('control', 'global', {'paused': False, 'actor': 'ygor'})
        self.cfg = config()
        self.file = Path(self.tmp.name) / 'campaign.json'
        self.file.write_text(json.dumps(self.cfg), encoding='utf-8')

    def invoke(self, *, uid=0, roles=None):
        with patch('thunder_os.apollo_linkedin_approve.os.geteuid', return_value=uid), \
             patch('thunder_os.apollo_linkedin_approve.load_roles', return_value=roles or {'ygor': 'admin'}), \
             patch('sys.stdout', new_callable=io.StringIO) as out, \
             patch('sys.stderr', new_callable=io.StringIO) as err:
            rc = main(['--campaign', 'campaign', '--actor', 'ygor', '--config', str(self.file)])
            return rc, out.getvalue(), err.getvalue()

    def test_root_admin_issues_real_campaign_approval(self):
        rc, output, _ = self.invoke()
        self.assertEqual(rc, 0)
        approval = json.loads(output)
        authority = CampaignApprovalAuthority(self.repo).require('campaign', approval['approval_id'], 'v1')
        self.assertEqual(authority['binding'], self.cfg['configuration']['apollo_linkedin'])
        self.assertEqual(authority['scope_hash'], approval['scope_hash'])

    def test_nonroot_and_nonadmin_never_write_approval(self):
        for uid, roles in [(1000, {'ygor': 'admin'}), (0, {'ygor': 'manager'}), (0, {'ygor': 'operator'})]:
            rc, _, _ = self.invoke(uid=uid, roles=roles)
            self.assertEqual(rc, 2)
        self.assertEqual(self.repo.list_state('campaign_config'), [])
        self.assertEqual(self.repo.list_state('approval'), [])

    def test_approval_failure_rolls_back_campaign_creation(self):
        self.repo.put_state('control', 'global', {'paused': True, 'actor': 'ygor'})
        rc, _, _ = self.invoke()
        self.assertEqual(rc, 2)
        self.assertEqual(self.repo.list_state('campaign_config'), [])
        self.assertEqual(self.repo.list_state('approval'), [])

    def test_updated_config_invalidates_prior_approval(self):
        first = approve_campaign(self.repo, 'campaign', self.cfg, 'ygor')
        newer = deepcopy(self.cfg); newer['campaign_version'] = 'v2'
        newer['configuration']['apollo_linkedin']['sequence_id'] = 'another'
        second = approve_campaign(self.repo, 'campaign', newer, 'ygor')
        self.assertNotEqual(first['scope_hash'], second['scope_hash'])
        with self.assertRaises(Exception):
            CampaignApprovalAuthority(self.repo).require('campaign', first['approval_id'], 'v1')
        self.assertEqual(CampaignApprovalAuthority(self.repo).require('campaign', second['approval_id'], 'v2')['binding']['sequence_id'], 'another')

    def test_duplicate_json_and_symlink_rejected(self):
        self.file.write_text('{"campaign_id":"x","campaign_id":"y"}')
        with self.assertRaises(ValueError): read_config(str(self.file))
        link = Path(self.tmp.name) / 'link.json'; link.symlink_to(self.file)
        with self.assertRaises(OSError): read_config(str(link))

    def test_other_existing_database_is_not_opened_or_migrated(self):
        other = Path(self.tmp.name) / 'other.sqlite'
        other.write_bytes(b'not an official database')
        before = other.read_bytes()
        with patch.dict(os.environ, {'PAINEL_DB': str(other)}):
            rc, _, error = self.invoke()
        self.assertEqual(rc, 2)
        self.assertIn('official_store_unavailable', error)
        self.assertEqual(other.read_bytes(), before)


if __name__ == '__main__': unittest.main()
