"""Panel activation contract, isolated state and mocked external boundaries only."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import painel_servidor as panel
from thunder_os.connectsafely_application import ConnectSafelyApplication
from thunder_os.connectsafely_composition import ConnectSafelyComposition
from thunder_os.connectsafely_live import build_panel_composition
from thunder_os.db import Repository, RepositoryConflict
from thunder_os.service import AuthorizationError


class PanelWiringTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = Repository(self.root / 'panel.db')
        self.source = self.root / 'source.json'
        self.source.write_text('{"fixture":true}')
        self.contract = self.root / 'contract.json'
        self.contract.write_text(json.dumps({
            'approved_by': 'ygor', 'approval_ref': 'fixture-only',
            'expires_at': 4102444800, 'sources': [self.pin(self.source)],
            'account_id': 'a' * 24, 'public_id': 'fixture-owner',
            'self_profile_id': 'urn:li:fsd_profile:fixture-owner',
            'quota_features': {'dm': 'MESSAGE', 'react_post': 'REACTION'},
        }))
        self.cfg = {
            'version': 1, 'mode': 'live', 'execution_enabled': True,
            'factory': 'builtin-connectsafely-v1', 'repository': str(self.repo.path),
            'live': {
                'social_enabled': False, 'execution_scope': 'fixture-panel-v1',
                'account_id': 'a' * 24, 'public_id': 'fixture-owner',
                'self_profile_id': 'urn:li:fsd_profile:fixture-owner',
                'api_credential_file': str(self.root / 'never-read-api'),
                'database_credential_file': str(self.root / 'never-read-db'),
                'database_identity': {'database': 'fixture', 'role': 'fixture'},
                'account_and_quota_evidence': self.pin(self.contract),
                'excluded_writers': [{'path': str(self.root / 'missing-writer'), 'sha256': '0' * 64}],
            },
        }
        self.config = self.root / 'config.json'
        self.environment = patch.dict(os.environ, {
            'THUNDER_EXTERNAL_EFFECTS_ENABLED': 'true',
            'THUNDER_OS_COPILOT_MODE': 'human_approval',
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        # Any accidental provider or PostgreSQL access is a hard test failure.
        for target in ('thunder_os.connectsafely_transport.ConnectSafelyHTTPS.__call__',
                       'thunder_os.connectsafely_social_transport.ConnectSafelySocialHTTPS.__call__',
                       'thunder_os.connectsafely_live.credential'):
            guard = patch(target, side_effect=AssertionError('external IO forbidden'))
            if target.endswith('.credential'):
                guard = patch(target, return_value=lambda: self.fail('credential read forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    @staticmethod
    def pin(path):
        return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def configure(self):
        self.config.write_text(json.dumps(self.cfg))
        pin = self.pin(self.config)
        os.environ['THUNDER_OS_CONNECTSAFELY_CONFIG'] = pin['path']
        os.environ['THUNDER_OS_CONNECTSAFELY_CONFIG_SHA256'] = pin['sha256']
        server = SimpleNamespace()
        with patch('thunder_os.connectsafely_read_targets.build_read_resolver', return_value=None):
            panel.configure_connectsafely_server(server, self.repo)
        return server.thunder_connectsafely_app

    def test_unconfigured_startup_is_disconnected(self):
        server = SimpleNamespace()
        with patch('thunder_os.connectsafely_read_targets.build_read_resolver', return_value=None):
            panel.configure_connectsafely_server(server, self.repo)
        self.assertFalse(server.thunder_connectsafely_app.status('ygor')['enqueue_enabled'])
        self.assertIsNone(server.thunder_connectsafely_app.provider)
        self.assertIsNone(self.repo.get_state('connectsafely_runtime_config', 'active'))

    def test_explicit_factory_keeps_existing_injection_contract(self):
        os.environ['THUNDER_OS_CONNECTSAFELY_CONFIG'] = '/missing'
        composition = ConnectSafelyComposition(ConnectSafelyApplication(self.repo))
        with patch('thunder_os.connectsafely_read_targets.build_read_resolver', return_value=None):
            result = panel.configure_connectsafely_server(SimpleNamespace(), self.repo, lambda repo: composition)
        self.assertIs(result, composition)

    def test_live_startup_binds_config_without_network_credentials_or_jobs(self):
        app = self.configure()
        self.assertIs(app.repo, self.repo)
        self.assertEqual('fixture-panel-v1', app.execution_scope)
        self.assertIsNotNone(app.direct)
        self.assertIsNone(app.social)
        self.assertEqual([], self.repo.list_state('connectsafely_job'))
        self.assertEqual(self.pin(self.config)['sha256'],
                         self.repo.get_state('connectsafely_runtime_config', 'active')['config_sha256'])
        self.assertFalse(app.process_one())

    def test_missing_or_wrong_pin_blocks_startup(self):
        self.config.write_text(json.dumps(self.cfg))
        for pin in ('', '0' * 64):
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                build_panel_composition(self.repo, config_path=str(self.config), config_sha256=pin)

    def test_path_must_be_absolute_and_not_symlink(self):
        self.config.write_text(json.dumps(self.cfg))
        link = self.root / 'link.json'
        link.symlink_to(self.config)
        for path in ('config.json', str(link)):
            with self.subTest(path=path), self.assertRaises(ValueError):
                build_panel_composition(self.repo, config_path=path, config_sha256=self.pin(self.config)['sha256'])

    def test_mode_factory_social_flag_and_scope_are_explicit(self):
        for field, value in (('mode', 'synthetic'), ('factory', {'path': '/plugin.py'})):
            original = self.cfg[field]
            self.cfg[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.configure()
            self.cfg[field] = original
        for field, value in (('execution_scope', None), ('execution_scope', ''),
                             ('social_enabled', None), ('social_enabled', 'false')):
            original = self.cfg['live'][field]
            self.cfg['live'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.configure()
            self.cfg['live'][field] = original

    def test_other_repository_and_changed_binding_are_rejected(self):
        self.cfg['repository'] = str(self.root / 'other.db')
        with self.assertRaisesRegex(ValueError, 'repository_configuration_mismatch'):
            self.configure()
        self.cfg['repository'] = str(self.repo.path)
        self.configure()
        self.cfg['live']['execution_scope'] = 'different-scope'
        with self.assertRaisesRegex(ValueError, 'runtime_configuration_mismatch'):
            self.configure()

    def test_disabled_effects_do_not_construct_live_ports(self):
        for enabled in (False, True):
            self.cfg['execution_enabled'] = enabled
            os.environ['THUNDER_EXTERNAL_EFFECTS_ENABLED'] = 'true' if not enabled else 'false'
            app = self.configure()
            self.assertIsNone(app.provider)
            self.assertFalse(app.status('ygor')['enqueue_enabled'])

    def test_evidence_source_and_identity_fail_closed(self):
        self.source.write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError, 'evidence_source_changed'):
            self.configure()
        self.source.write_text('{"fixture":true}')
        self.cfg['live']['public_id'] = 'different-owner'
        with self.assertRaisesRegex(ValueError, 'account_evidence_mismatch'):
            self.configure()

    def test_dynamic_evidence_and_writer_exclusions_remain_required(self):
        app = self.configure()
        self.assertFalse(app.action_readiness('dm')['ready'])
        with patch('thunder_os.legacy_writer_exclusion.verify_legacy_exclusion'), \
                patch('thunder_os.connectsafely_live.direct_ledger_readiness', return_value={'ready': True}):
            self.assertTrue(app.action_readiness('dm')['ready'])
            self.source.write_text('{"changed":true}')
            self.assertFalse(app.action_readiness('dm')['ready'])

    def test_human_mode_and_exclusions_apply_with_social_enabled(self):
        self.cfg['live']['excluded_writers'] = []
        self.cfg['live'].update(social_enabled=True, inventory={
            'writers': [], 'expected_writer_ids': [],
            'inventory_path': str(self.root / 'inventory.json'), 'inventory_sha256': '0' * 64})
        with patch('thunder_os.connectsafely_live.PostgresSocialCoordinator') as coordinator:
            coordinator.return_value.readiness.return_value = {'verified': True}
            app = self.configure()
            self.assertTrue(app.action_readiness('comment')['ready'])
            os.environ['THUNDER_OS_COPILOT_MODE'] = 'automatic'
            for action in ('dm', 'invite', 'comment', 'reply', 'react_post'):
                self.assertFalse(app.action_readiness(action)['ready'])
            os.environ['THUNDER_OS_COPILOT_MODE'] = 'human_approval'
        self.cfg['live']['excluded_writers'] = []
        # Fresh repository avoids changing a previously bound operational config.
        self.repo = Repository(self.root / 'social.db')
        self.cfg['repository'] = str(self.repo.path)
        with patch('thunder_os.connectsafely_live.PostgresSocialCoordinator'), \
                patch('thunder_os.connectsafely_live.direct_ledger_readiness', return_value={'ready': True}):
            self.assertFalse(self.configure().action_readiness('invite')['ready'])

    def route(self, app, operation, body, actor='ygor'):
        handler = object.__new__(panel.Handler)
        handler.server = SimpleNamespace(thunder_service=SimpleNamespace(repository=self.repo),
                                         thunder_connectsafely_app=app)
        handler.command = 'POST'
        handler.thunder_actor = lambda: actor
        handler.corpo = lambda: body
        handler.responder = lambda status, result: (status, result)
        with patch.object(panel, 'restricted_mode', return_value=False):
            return handler.thunder_api(urlsplit('/api/os/v1/integrations/connectsafely/' + operation))

    def test_confirmation_dispatches_exact_job_and_ambiguity_never_retries(self):
        app = self.configure()
        plan = {'account_id': 'a' * 24, 'account_public_id': 'fixture-owner',
                'action': 'dm', 'recipient_public_id': 'fixture-target',
                'recipient_profile_urn': 'urn:li:fsd_profile:fixture-target',
                'channel': 'linkedin', 'messaging_channel': 'linkedin_inbox', 'content': 'Fixture only'}
        def uncertain(review, *, actor, dispatch_guard):
            dispatch_guard()
            return {'status': 'uncertain', 'retry_write': False}
        with patch.object(app.direct, '_identity'), \
                patch.object(app, 'action_readiness', return_value={'ready': True}), \
                patch.object(app.direct, 'execute', side_effect=uncertain) as execute:
            first = self.route(app, 'preview', plan)[1]
            other = self.route(app, 'preview', {**plan, 'content': 'Other fixture'})[1]
            review = lambda p: {'preview_id': p['id'], 'expected_version': p['version'], 'review_hash': p['review_hash']}
            other_job = app.enqueue(review(other), 'ygor')
            self.assertEqual(0, execute.call_count)
            with self.assertRaises(RepositoryConflict):
                self.route(app, 'execute', {**review(first), 'review_hash': 'wrong'})
            with self.assertRaises(AuthorizationError):
                self.route(app, 'execute', review(first), actor='hermes')
            status, result = self.route(app, 'execute', review(first))
            self.assertEqual(200, status)
            self.assertEqual('needs_reconciliation', result['state'])
            self.assertEqual('queued', self.repo.get_state('connectsafely_job', other_job['job_id'])['state'])
            self.route(app, 'execute', review(first))
            self.assertEqual(1, execute.call_count)
            self.assertFalse(app.process_one())


if __name__ == '__main__':
    unittest.main()
