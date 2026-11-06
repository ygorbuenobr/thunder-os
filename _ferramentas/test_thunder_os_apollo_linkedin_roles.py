import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import painel_servidor as panel
from thunder_os import apollo_linkedin_roles as roles
from thunder_os.db import Repository
from thunder_os.apollo_linkedin_attention import reservation_attention_item, reservation_resolution


class RootManagedRolesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve() / 'roles.json'
        self.path.write_text('{"ygor":"admin","manager":"manager","reader":"operator"}')
        self.path.chmod(0o644)
        self.real_fstat = os.fstat
        self.parents = patch.object(roles, '_root_controlled_parents', return_value=True)
        self.parents.start(); self.addCleanup(self.parents.stop)
        def trusted_fstat(fd):
            data = list(self.real_fstat(fd)); data[4] = 0
            return os.stat_result(data)
        self.file_owner = patch.object(roles.os, 'fstat', side_effect=trusted_fstat)
        self.file_owner.start(); self.addCleanup(self.file_owner.stop)

    def test_roles_admin_manager_and_operator(self):
        self.assertTrue(roles.authorize_manager('ygor', str(self.path)))
        self.assertTrue(roles.authorize_manager('manager', str(self.path)))
        self.assertFalse(roles.authorize_manager('reader', str(self.path)))
        self.assertFalse(roles.authorize_manager('unknown', str(self.path)))

    def test_malformed_unsafe_or_missing_fail_closed(self):
        for text in ('{}', '{"ygor":"owner"}', '{"ygor":"admin","ygor":"admin"}', '[]'):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaises(roles.RolesUnavailable): roles.load_roles(str(self.path))
        self.path.write_text('{"ygor":"admin"}')
        self.path.chmod(0o666)
        with self.assertRaises(roles.RolesUnavailable): roles.load_roles(str(self.path))
        self.path.chmod(0o644)
        link = self.path.parent / 'link'; link.symlink_to(self.path)
        with self.assertRaises(roles.RolesUnavailable): roles.load_roles(str(link))
        with self.assertRaises(roles.RolesUnavailable): roles.load_roles(str(self.path.parent / 'missing'))
        with patch.object(roles, '_root_controlled_parents', return_value=False):
            with self.assertRaises(roles.RolesUnavailable): roles.load_roles(str(self.path))


class RoleRoutesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name).resolve() / 'panel.db'
        db.touch()
        official = patch('thunder_os.db.OFFICIAL_PANEL_DB', db)
        official.start(); self.addCleanup(official.stop)
        self.repo = Repository(str(db))
        self.repo.reserve_apollo_linkedin('company', 'task', 'linkedin_step_connect', 'account')
        self.repo.finish_apollo_linkedin('company', 'task', 'linkedin_step_connect', 'uncertain', 'timeout')
        self.handler = object.__new__(panel.Handler)
        self.handler.server = SimpleNamespace(thunder_service=SimpleNamespace(repository=self.repo),
                                              thunder_apollo_linkedin_authorized_company='company')
        self.handler.command = 'POST'
        self.handler.thunder_proxy_actor = lambda: 'ygor'
        self.handler.thunder_actor = lambda: 'ygor'
        self.handler.responder = lambda status, value: (status, value)
        self.body = {'company': 'company', 'task_id': 'task', 'action_type': 'linkedin_step_connect',
                     'decision': 'close', 'justification': 'Verificado manualmente'}
        self.handler.corpo = lambda: self.body
        self.route = urlsplit('/api/os/v1/integrations/apollo/linkedin-uncertain/resolve')

    def request(self): return self.handler.thunder_api(self.route)

    def test_missing_roles_503_operator_403_and_no_side_effects(self):
        with patch.object(roles, 'authorize_manager', side_effect=roles.RolesUnavailable('bad')):
            with self.assertRaises(panel.Erro) as error: self.request()
            self.assertEqual(503, error.exception.status)
        with patch.object(roles, 'authorize_manager', return_value=False):
            with self.assertRaises(panel.Erro) as error: self.request()
            self.assertEqual(403, error.exception.status)
        self.assertEqual('uncertain', self.repo.get_apollo_linkedin_reservation('company', 'task', 'linkedin_step_connect')['state'])

    def test_company_scope_required_and_cross_tenant_denied(self):
        with patch.object(roles, 'authorize_manager', return_value=True):
            del self.handler.server.thunder_apollo_linkedin_authorized_company
            with self.assertRaises(panel.Erro) as error: self.request()
            self.assertEqual(503, error.exception.status)
            self.handler.server.thunder_apollo_linkedin_authorized_company = 'other-company'
            with self.assertRaises(panel.Erro) as error: self.request()
            self.assertEqual(403, error.exception.status)

    def test_proxy_and_nonce_identity_must_match(self):
        with patch.object(roles, 'authorize_manager', return_value=True):
            self.handler.thunder_actor = lambda: 'other'
            with self.assertRaises(panel.Erro) as error: self.request()
            self.assertEqual(401, error.exception.status)
        self.assertEqual('uncertain', self.repo.get_apollo_linkedin_reservation('company', 'task', 'linkedin_step_connect')['state'])

    def test_empty_justification_rejected_and_audited_resolution_has_no_send(self):
        with patch.object(roles, 'authorize_manager', return_value=True):
            self.body['justification'] = '  '
            with self.assertRaises(panel.Erro) as error: self.request()
            self.assertEqual(400, error.exception.status)
            self.body['justification'] = 'Verificado manualmente'
            status, result = self.request()
        self.assertEqual(200, status)
        self.assertFalse(result['execution_enabled'])
        self.assertEqual('uncertain', self.repo.get_apollo_linkedin_reservation('company', 'task', 'linkedin_step_connect')['state'])
        with self.repo._connect() as db:
            row = db.execute("SELECT payload FROM thunder_events WHERE type='apollo_linkedin_uncertain_resolved'").fetchone()
        self.assertIsNotNone(row)
        event = json.loads(row[0]); self.assertEqual('ygor', event['actor'])
        self.assertEqual('Verificado manualmente', event['justification'])

    def test_canary_remains_blocked_without_approved_preview_binding(self):
        self.route = urlsplit('/api/os/v1/integrations/apollo/linkedin-canary')
        self.body = {'company': 'company', 'task_id': 'task', 'account_id': 'account',
                     'recipient': 'lead', 'text_hash': 'a' * 64}
        with patch.object(roles, 'authorize_manager', return_value=True):
            with self.assertRaises(panel.Erro) as error: self.request()
        self.assertEqual(503, error.exception.status)
        self.assertEqual([], self.repo.list_state('apollo_linkedin_canary'))

    def test_closed_uncertainty_projection_requires_exact_scope_and_evidence(self):
        self.repo.resolve_apollo_linkedin_uncertain('company', 'task', 'linkedin_step_connect',
                                                   'close', 'Verificado', 'ygor')
        row = self.repo.get_apollo_linkedin_reservation('company', 'task', 'linkedin_step_connect')
        service = self.handler.server.thunder_service
        decision = reservation_resolution(service, row, 'company')
        item = reservation_attention_item(row, decision)
        self.assertEqual('uncertain_closed', item['reason'])
        self.assertEqual('resolved', item['attention_level'])
        self.assertEqual('uncertain', row['state'])
        for scope in (None, 'other-company'):
            self.assertIsNone(reservation_resolution(service, row, scope))
        changed = dict(row, updated_at='different')
        self.assertIsNone(reservation_resolution(service, changed, 'company'))
        self.repo.reserve_apollo_linkedin('company', 'pending', 'linkedin_step_connect', 'account')
        self.repo.finish_apollo_linkedin('company', 'pending', 'linkedin_step_connect', 'uncertain', 'timeout')
        pending = self.repo.get_apollo_linkedin_reservation('company', 'pending', 'linkedin_step_connect')
        pending_item = reservation_attention_item(pending, reservation_resolution(service, pending, 'company'))
        self.assertEqual('timeout', pending_item['reason'])
        self.assertNotIn('attention_level', pending_item)


if __name__ == '__main__': unittest.main()
