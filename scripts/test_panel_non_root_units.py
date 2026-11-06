#!/usr/bin/env python3
"""Offline checks for non-deployed non-root panel/broker candidates."""
import configparser
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / 'runtime' / 'systemd'
PANEL = SYSTEMD / 'painel-thunder-non-root.conf.candidate'
BROKER = SYSTEMD / 'thunder-os-connectsafely-read-broker.service.candidate'
BROKER_ENV = SYSTEMD / 'panel-li-reader.env.candidate'
SERVICE_ENV = SYSTEMD / 'connectsafely-read-broker.env.candidate'


def parse(path):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    # systemd accepts repeated LoadCredential directives; ConfigParser does not.
    lines = [line for line in path.read_text(encoding='utf-8').splitlines()
             if not line.startswith('LoadCredential=')]
    parser.read_string('\n'.join(lines))
    return parser['Service']


class NonRootCandidateTests(unittest.TestCase):
    def setUp(self):
        self.panel, self.broker = parse(PANEL), parse(BROKER)

    def test_distinct_nonroot_users_and_restricted_privileges(self):
        self.assertEqual('thunder-panel', self.panel['User'])
        self.assertEqual('thunder-li-reader', self.broker['User'])
        self.assertNotEqual(self.panel['User'], self.broker['User'])
        for service in (self.panel, self.broker):
            self.assertNotEqual('root', service['User'])
            self.assertEqual('yes', service['NoNewPrivileges'])
            self.assertEqual('strict', service['ProtectSystem'])
            self.assertEqual('', service['CapabilityBoundingSet'])
            self.assertEqual('', service['AmbientCapabilities'])
            self.assertEqual('yes', service['RestrictSUIDSGID'])
            self.assertEqual('yes', service['PrivateTmp'])

    def test_only_narrow_readwrite_paths(self):
        for service in (self.panel, self.broker):
            paths = service['ReadWritePaths'].split()
            self.assertTrue(paths)
            for path in paths:
                normalized = Path(path)
                self.assertTrue(normalized.is_absolute())
                self.assertNotIn('..', normalized.parts)
                self.assertNotIn(str(normalized), ('/', '/root'))
        self.assertEqual('0007', self.panel['UMask'])
        self.assertEqual('0750', self.broker['RuntimeDirectoryMode'])
        # In-process read accepted (05/11/2026): the panel does not join the broker socket group.
        self.assertEqual(['thunder-panel-db', 'www-data'], self.panel['SupplementaryGroups'].split())
        self.assertEqual('thunder-li-socket', self.broker['Group'])

    def test_candidates_contain_no_literal_secrets_and_broker_fails_closed(self):
        common = {'User', 'Group', 'UMask', 'NoNewPrivileges', 'ProtectSystem', 'ProtectHome',
                  'PrivateTmp', 'CapabilityBoundingSet', 'AmbientCapabilities', 'RestrictSUIDSGID',
                  'ReadWritePaths', 'Environment', 'EnvironmentFile'}
        self.assertEqual(common - {'Environment', 'EnvironmentFile'} | {'SupplementaryGroups', 'UnsetEnvironment'},
                         set(self.panel))
        self.assertEqual(common | {'Type', 'RuntimeDirectory', 'RuntimeDirectoryMode',
                                   'RestrictAddressFamilies', 'ExecStart', 'Restart', 'WorkingDirectory'},
                         set(self.broker))
        for path in (PANEL, BROKER, BROKER_ENV, SERVICE_ENV):
            for line in path.read_text(encoding='utf-8').splitlines():
                if not line or line.lstrip().startswith('#'):
                    continue
                self.assertFalse(re.search(r'(?i)(?:api[_-]?key|secret|token|password)\s*=\s*\S+', line), line)
        self.assertIn('THUNDER_APOLLO_LI_BROKER_UID=0', BROKER_ENV.read_text(encoding='utf-8').splitlines())
        self.assertEqual('/usr/bin/python3 -m thunder_os.connectsafely_read_broker_service', self.broker['ExecStart'])
        self.assertNotIn('Environment', self.panel)
        self.assertNotIn('EnvironmentFile', self.panel)
        self.assertEqual('PYTHONPATH=/opt/thunder-os/current/_ferramentas', self.broker['Environment'])
        self.assertEqual('/etc/thunder-os/connectsafely-read-broker.env', self.broker['EnvironmentFile'])
        self.assertFalse(any(key in self.broker for key in ('LoadCredential', 'SetCredential')))
        self.assertFalse('SetCredential' in self.panel)
        lines = [line for line in PANEL.read_text(encoding='utf-8').splitlines()
                 if line.startswith('LoadCredential=')]
        expected = {
            'apollo_api_key': '/root/.config/thunder/credentials/apollo_api_key.txt',
            'instantly_api_key': '/etc/thunder-os/instantly_api_key',
            'instantly_webhook_secret': '/etc/thunder-os/instantly_webhook_secret',
            'agenttrail_ingress_token': '/etc/thunder-os/agenttrail-ingress.token',
            'apollo_reader_enabled': '/etc/thunder-os/apollo_reader_enabled',
            'instantly_reader_enabled': '/etc/thunder-os/instantly_reader_enabled',
            'connectsafely_token': '/etc/thunder-os/connectsafely.token',
            'social_db_dsn': '/etc/thunder-os/social-db.dsn',
            'connectsafely_config': '/etc/thunder-os/direct-runtime-20261103.json',
            'hubspot_access': '/var/lib/thunder-os-integrations/credentials/hubspot_access',
            'seqd_bridge': '/var/lib/thunder-os-integrations/credentials/seqd_bridge',
            'seqd_email_ledger': '/var/lib/thunder-os-integrations/credentials/seqd_email_ledger',
        }
        self.assertEqual(len(expected), len(lines))
        self.assertEqual({f'LoadCredential={key}:{value}' for key, value in expected.items()}, set(lines))
        self.assertEqual('THUNDER_OS_HUBSPOT_CREDENTIAL_FILE THUNDER_OS_SEQD_CREDENTIAL_FILE '
                         'THUNDER_OS_SEQD_EMAIL_LEDGER_CREDENTIAL_FILE', self.panel['UnsetEnvironment'])
        env_lines = [line for line in BROKER_ENV.read_text(encoding='utf-8').splitlines()
                     if line.strip() and not line.lstrip().startswith('#')]
        self.assertEqual(['THUNDER_APOLLO_LI_BROKER_UID=0'], env_lines)
        service_lines = [line for line in SERVICE_ENV.read_text(encoding='utf-8').splitlines()
                         if line.strip() and not line.lstrip().startswith('#')]
        self.assertEqual([
            'THUNDER_LI_BROKER_COMPANY=', 'THUNDER_LI_BROKER_ACCOUNT=',
            'THUNDER_LI_BROKER_PUBLIC_ID=', 'THUNDER_LI_BROKER_PANEL_UID=0',
            'THUNDER_LI_BROKER_SOCKET_GID=0',
            'THUNDER_LI_BROKER_SOCKET=/run/thunder-li-reader/account.sock',
            'THUNDER_LI_BROKER_CREDENTIAL_FILE=/etc/thunder-os/connectsafely-read-broker.key',
        ], service_lines)


if __name__ == '__main__':
    unittest.main()
