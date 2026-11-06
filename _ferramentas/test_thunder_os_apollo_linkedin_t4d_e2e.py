"""Offline T4d integration through actual issuers, factory and coordinators."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest

from thunder_os.db import Repository
from thunder_os.connectsafely_client import ConnectSafelyClient
from thunder_os import apollo_linkedin_approve as approve
from thunder_os import apollo_linkedin_canary_issue as issue
from thunder_os import apollo_linkedin_live_executor as runtime
from test_thunder_os_apollo_linkedin_authority import config as base_config

ACCOUNT = 'a' * 24
OWNER = 'urn:li:fsd_profile:OWNER_FIXTURE'
RECIPIENT = 'urn:li:fsd_profile:RECIPIENT_FIXTURE'
TEXT = 'Synthetic message for integration test.'


def config():
    cfg = base_config()
    cfg['configuration']['required_custom_field_id'] = 'abm_field'
    return cfg


class Ledger:
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as con:
            con.execute('CREATE TABLE toques(canal TEXT, status TEXT, atualizado_em TEXT, pessoa_chave TEXT, criado_em TEXT)')

    @contextmanager
    def _banco(self):
        with sqlite3.connect(self.path) as con:
            yield con, '?'

    def add_recent(self):
        with sqlite3.connect(self.path) as con:
            now = datetime.now(timezone.utc).isoformat()
            con.execute('INSERT INTO toques VALUES (?, ?, ?, ?, ?)',
                        ('linkedin', 'feito', now, 'fixture-person', now))


class Scenario:
    def __init__(self):
        self.apollo_calls = []
        self.linkedin_calls = []
        self.replied = False
        self.remaining = 1
        self.status = 'scheduled'
        self.note = None
        self.on_complete = lambda: None

    def task(self):
        return {'id': 'fixture-task', 'type': 'linkedin_step_message', 'status': self.status,
                'emailer_campaign_id': 'seq', 'user_id': 'owner', 'contact_id': 'contact',
                'contact': {'id': 'contact', 'linkedin_url': 'https://www.linkedin.com/in/fixture-person/',
                            'typed_custom_fields': {'abm_field': TEXT}},
                'linkedin_emailer_template': {'body_text': TEXT}, 'note': self.note}

    def apollo(self, method, path, body, headers, timeout):
        self.apollo_calls.append((method, path, body))
        if path.endswith('/api_profile'):
            obj = {'user': {'id': 'actor', 'team_id': 'team'}}
        elif '/contacts/' in path:
            obj = {'contact': {'id': 'contact', 'contact_campaign_statuses': [
                {'emailer_campaign_id': 'seq', 'status': 'active', 'replied': self.replied}]}}
        else:
            if method == 'POST':
                self.on_complete()
                self.note = json.loads(body)['note']
                self.status = 'completed'
            obj = {'task': self.task()}
        return 200, json.dumps(obj).encode()

    def linkedin(self, method, path, query=None, body=None):
        self.linkedin_calls.append((method, path, body))
        if path.endswith('/accounts'):
            return 200, [{'id': ACCOUNT, 'publicId': 'sender', 'status': 'AVAILABLE',
                          'enabled': True, 'apiSeatAllocated': True}]
        if '/relationship/' in path:
            return 200, {'accountId': ACCOUNT, 'profileUrn': RECIPIENT, 'status': 'CONNECTED',
                         'connected': True, 'invitationSent': False, 'invitationReceived': False}
        if path.endswith('/quota'):
            return 200, {'accountId': ACCOUNT, 'quotas': [
                {'feature': 'test_dm', 'window': 'daily', 'used': 1 - self.remaining, 'limit': 1,
                 'remaining': self.remaining, 'limitReached': self.remaining == 0,
                 'resetAt': '2099-01-01T00:00:00Z'}]}
        if '/exists/' in path:
            return 200, {'success': True, 'accountId': ACCOUNT, 'profileUrn': RECIPIENT,
                         'hasConversation': False}
        if method == 'POST' and path.endswith('/conversations/send'):
            return 200, {'success': True, 'accountId': ACCOUNT, 'recipientProfileUrn': RECIPIENT,
                         'sentMessage': {'messageUrn': 'urn:li:messagingMessage:FIXTURE',
                                         'text': TEXT, 'sentAt': int((time.time() + 1) * 1000)}}
        raise AssertionError('unexpected fake request')


class T4dEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        self.path = directory / 'panel.db'; self.path.touch()
        self.ledger = Ledger(directory / 'ledger.db')
        self.fake = Scenario()
        self.env = patch.dict(os.environ, {'PAINEL_DB': str(self.path),
            'THUNDER_APOLLO_LI_OFFICIAL_DB': str(self.path),
            'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true',
            'THUNDER_APOLLO_LI_COMPANY': 'company', 'THUNDER_APOLLO_LI_TEAM': 'team',
            'THUNDER_APOLLO_LI_API_ACTOR': 'actor',
            'THUNDER_OS_CONNECTSAFELY_CONFIG': '/synthetic/config',
            'THUNDER_OS_CONNECTSAFELY_CONFIG_SHA256': '0' * 64})
        self.env.start(); self.addCleanup(self.env.stop)
        self.repo = Repository(self.path)
        self.repo.put_state('control', 'global', {'paused': False, 'actor': 'ygor'})
        def assert_receipt_was_persisted():
            row = self.repo.get_apollo_linkedin_reservation('company', 'fixture-task', 'linkedin_step_message')
            self.assertEqual(row['state'], 'confirmed')
            self.assertEqual(row['receipt']['provider_id'], 'connectsafely:urn:li:messagingMessage:FIXTURE')
        self.fake.on_complete = assert_receipt_was_persisted
        self.client = ConnectSafelyClient(ACCOUNT, 'sender', request=self.fake.linkedin,
            self_profile_id=OWNER, dm_quota_feature='test_dm')
        self.resolver = lambda _: {'resolved': True, 'read_only': True, 'source': 'provider_observation',
            'target': {'account_id': ACCOUNT, 'account_public_id': 'sender',
                       'recipient_public_id': 'fixture-person', 'recipient_profile_urn': RECIPIENT}}
        self.patchers = [
            patch('thunder_os.apollo_linkedin_reader._https_request', self.fake.apollo),
            patch('thunder_os.apollo_linkedin_reply_coverage._contact_request', self.fake.apollo),
            patch('thunder_os.apollo_linkedin_completion._request', self.fake.apollo),
            patch('thunder_os.apollo_linkedin_completion._complete_request', self.fake.apollo),
            patch('thunder_os.connectsafely_live.credential', return_value=lambda: 'test-token'),
            patch('thunder_os.credential_paths.credential_path', side_effect=lambda name, fallback: fallback),
            patch('thunder_os.connectsafely_live.build_panel_composition', return_value=SimpleNamespace(
                application=SimpleNamespace(provider=SimpleNamespace(direct_client=self.client),
                                            direct=SimpleNamespace(ledger=self.ledger)))),
            patch('thunder_os.apollo_linkedin_live_executor.hermes_unit_inactive', return_value=True),
            patch('socket.create_connection', side_effect=AssertionError('network prohibited')),
        ]
        for p in self.patchers:
            p.start(); self.addCleanup(p.stop)
        cfg_path = directory / 'campaign.json'; cfg_path.write_text(json.dumps(config()))
        with patch('thunder_os.apollo_linkedin_approve.os.geteuid', return_value=0), \
             patch('thunder_os.apollo_linkedin_approve.load_roles', return_value={'ygor': 'admin'}), \
             patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(0, approve.main(['--campaign', 'campaign', '--actor', 'ygor', '--config', str(cfg_path)]))
            self.approval = json.loads(out.getvalue())
        with patch('thunder_os.apollo_linkedin_canary_issue.authorize_actor'):
            self.authorization = issue.issue(self.repo, task_id='fixture-task', actor='ygor',
                company='company', team='team', api_actor='actor', loader=lambda: 'test-token',
                resolver=self.resolver, roles_file='/synthetic/roles')['id']

    def run_executor(self):
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            code = runtime.main(['--task', 'fixture-task', '--authorization', self.authorization, '--execute'])
            return code, json.loads(out.getvalue())

    def posts(self):
        return [call for call in self.fake.linkedin_calls if call[0] == 'POST']

    def test_unconnected_message_skips_apollo_without_linkedin_post(self):
        linkedin = self.fake.linkedin
        apollo = self.fake.apollo
        def not_connected(method, path, query=None, body=None):
            if '/relationship/' in path:
                self.fake.linkedin_calls.append((method, path, body))
                return 200, {'accountId': ACCOUNT, 'profileUrn': RECIPIENT, 'status': 'INVITATION_SENT',
                             'connected': False, 'invitationSent': True, 'invitationReceived': False}
            return linkedin(method, path, query, body)
        def skip(method, path, body, headers, timeout):
            if method == 'POST' and path.endswith('/skip'):
                self.fake.apollo_calls.append((method, path, body))
                self.fake.note = json.loads(body)['note']
                self.fake.status = 'skipped'
                return 200, json.dumps({'task': self.fake.task()}).encode()
            return apollo(method, path, body, headers, timeout)
        self.client.request = not_connected
        with patch('thunder_os.apollo_linkedin_completion._skip_request', skip):
            _, result = self.run_executor()
        self.assertEqual('skipped', result['state'])
        self.assertEqual('não conectado, pulado', self.fake.note)
        self.assertEqual([], self.posts())
        self.assertEqual(1, sum(c[0] == 'POST' and c[1].endswith('/skip') for c in self.fake.apollo_calls))

    def test_no_email_real_components_and_fake_http_prove_receipt_then_completion(self):
        code, result = self.run_executor()
        self.assertEqual((code, result['state']), (0, 'confirmed'))
        self.assertEqual(len(self.posts()), 1)
        row = self.repo.get_apollo_linkedin_reservation('company', 'fixture-task', 'linkedin_step_message')
        self.assertEqual(row['receipt']['provider_id'], 'connectsafely:urn:li:messagingMessage:FIXTURE')
        self.assertEqual(row['apollo_status'], 'confirmed')
        self.assertEqual(sum(c[0] == 'POST' for c in self.fake.apollo_calls), 1)
        self.assertEqual(self.repo.get_state('apollo_linkedin_canary', self.authorization)['state'], 'consumed')
        self.run_executor()
        self.assertEqual(len(self.posts()), 1)

    def test_hermes_active_blocks(self):
        with patch('thunder_os.apollo_linkedin_live_executor.hermes_unit_inactive', return_value=False):
            self.assertEqual(self.run_executor()[1]['reason'], 'safety_unverified')
        self.assertEqual(self.posts(), [])

    def test_quota_zero_blocks(self):
        self.fake.remaining = 0
        self.assertEqual(self.run_executor()[1]['reason'], 'safety_unverified')
        self.assertEqual(self.posts(), [])

    def test_recent_action_blocks_pacing(self):
        self.ledger.add_recent()
        self.assertEqual(self.run_executor()[1]['reason'], 'safety_unverified')
        self.assertEqual(self.posts(), [])

    def test_reply_blocks_and_persists_stop(self):
        self.fake.replied = True
        self.assertEqual(self.run_executor()[1]['reason'], 'human_reply')
        self.assertEqual(self.posts(), [])
        self.assertEqual(len(self.repo.list_state('apollo_linkedin_reply_stop')), 1)

    def test_execution_disabled_makes_no_http_request(self):
        self.fake.apollo_calls.clear(); self.fake.linkedin_calls.clear()
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'false'}):
            code, result = self.run_executor()
            self.assertEqual(code, 2)
            self.assertIn(result.get('state', result.get('status')), ('blocked',))
        self.assertEqual(self.fake.apollo_calls, [])
        self.assertEqual(self.fake.linkedin_calls, [])


if __name__ == '__main__': unittest.main()
