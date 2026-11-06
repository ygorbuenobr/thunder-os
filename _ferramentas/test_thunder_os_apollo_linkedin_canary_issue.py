"""Canary issuer tests use synthetic records, fake reads and temporary SQLite."""
from pathlib import Path
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from thunder_os import apollo_linkedin_canary_issue as cli
from thunder_os.apollo_linkedin_attention import EXCLUDED_TASK
from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.db import Repository, RepositoryConflict
from thunder_os.service import ThunderService
from test_thunder_os_apollo_linkedin_authority import config


class CanaryIssuerTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'panel.db'; path.touch()
        env = patch.dict('os.environ', {'THUNDER_APOLLO_LI_OFFICIAL_DB': str(path)})
        env.start(); self.addCleanup(env.stop)
        self.repo = Repository(path)
        service = ThunderService(self.repo)
        CampaignRegistry(self.repo).register(config(), 'ygor', 0)
        self.repo.put_state('control', 'global', {'paused': False, 'actor': 'ygor'})
        review = service.apollo_campaign_approval_review('campaign')
        self.approval = service.approve_apollo_campaign_version('campaign', 'ygor',
            execution_confirmation=review['execution_confirmation'])
        self.task = {'id': 'task', 'type': 'linkedin_step_message', 'status': 'scheduled',
            'emailer_campaign_id': 'seq', 'user_id': 'owner', 'contact_id': 'contact',
            'contact': {'id': 'contact', 'linkedin_url': 'https://linkedin.com/in/person'},
            'linkedin_emailer_template': {'body_text': 'Mensagem sintética'}}
        self.observation = {'resolved': True, 'read_only': True, 'source': 'provider_observation',
            'target': {'account_id': 'a'*24, 'account_public_id': 'sender',
                       'recipient_public_id': 'person', 'recipient_profile_urn': 'urn:li:fsd_profile:abc'}}

    def issue(self, task_id='task'):
        outer = self
        class Reader:
            def __init__(self, loader, *, team_id, api_actor_id, owner_ids):
                self.team_id, self.owner_ids = team_id, frozenset(owner_ids)
            def get(self, task_id):
                return dict(outer.task)
        with patch.object(cli, 'ApolloLinkedInReader', Reader), \
             patch.object(cli.os, 'geteuid', return_value=0), \
             patch.object(cli, 'load_roles', return_value={'ygor': 'admin'}):
            return cli.issue(self.repo, task_id=task_id, actor='ygor', company='company', team='team',
                api_actor='actor', loader=lambda: 'fake', resolver=lambda _: self.observation,
                roles_file='/fake/roles.json')

    def test_issue_is_bound_and_consumable_once(self):
        result = self.issue()
        row = self.repo.get_state('apollo_linkedin_canary', result['id'])
        self.assertEqual((row['state'], row['approval_id'], row['campaign_version']),
                         ('issued', self.approval['id'], 'v1'))
        self.assertEqual(row['member_urn'], 'urn:li:fsd_profile:abc')
        args = (result['id'], 'company', 'task', 'linkedin_step_message', 'a'*24,
                'person', row['text_hash'], 'v1')
        kw = {'member_urn': row['member_urn'], 'approval_id': self.approval['id'], 'sequence_id': 'seq'}
        self.repo.reserve_apollo_linkedin_with_canary(*args, **kw)
        with self.assertRaises(RepositoryConflict):
            self.repo.reserve_apollo_linkedin_with_canary(*args, **kw)

    def test_wrong_actor_or_role_cannot_issue(self):
        with patch.object(cli.os, 'geteuid', return_value=0), \
             patch.object(cli, 'load_roles', return_value={'ygor': 'manager'}):
            with self.assertRaises(ValueError): cli.authorize_actor('ygor', '/fake/roles.json')
        with patch.object(cli.os, 'geteuid', return_value=1000):
            with self.assertRaises(ValueError): cli.authorize_actor('ygor', '/fake/roles.json')
        with patch.object(cli.os, 'geteuid', return_value=0), \
             patch.object(cli, 'load_roles', return_value={'other': 'admin'}):
            with self.assertRaises(ValueError): cli.authorize_actor('other', '/fake/roles.json')

    def test_fixture_and_unknown_approval_never_issue(self):
        with self.assertRaises(ValueError): self.issue(EXCLUDED_TASK)
        self.task['emailer_campaign_id'] = 'different'
        with self.assertRaises(ValueError): self.issue()
        self.assertEqual(self.repo.list_state('apollo_linkedin_canary'), [])

    def test_account_mismatch_never_issue(self):
        self.observation['target']['account_id'] = 'b'*24
        with self.assertRaises(Exception): self.issue()
        self.assertEqual(self.repo.list_state('apollo_linkedin_canary'), [])

    def test_repeated_approval_of_same_revision_remains_usable(self):
        service = ThunderService(self.repo)
        review = service.apollo_campaign_approval_review('campaign')
        newer = service.approve_apollo_campaign_version('campaign', 'ygor',
            execution_confirmation=review['execution_confirmation'])
        result = self.issue()
        self.assertEqual(result['approval_id'], newer['id'])

    def test_cli_rejects_non_root_before_opening_database(self):
        with patch.object(cli.os, 'geteuid', return_value=1000), \
             patch.object(cli, 'Repository') as repo, \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(['--task', 'task', '--actor', 'ygor']), 2)
            self.assertEqual(json.loads(output.getvalue())['status'], 'blocked')
        repo.assert_not_called()

    def test_cli_never_opens_or_migrates_a_different_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / 'different.db'
            other.write_bytes(b'synthetic data outside the official database')
            before = other.read_bytes()
            with patch.object(cli, 'authorize_actor'), \
                 patch.dict('os.environ', {'PAINEL_DB': str(other)}), \
                 patch.object(cli, 'Repository') as repo, \
                 patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(cli.main(['--task', 'task', '--actor', 'ygor']), 2)
            repo.assert_not_called()
            self.assertEqual(other.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
