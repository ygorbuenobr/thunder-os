"""Panel activation contract, isolated state and mocked external boundaries only."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
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

    def test_database_session_opt_in_is_connection_local(self):
        for flag in (None, False, True):
            self.repo = Repository(self.root / f'session-{flag}.db')
            self.cfg['repository'] = str(self.repo.path)
            if flag is None: self.cfg['live'].pop('database_session_read_write', None)
            else: self.cfg['live']['database_session_read_write'] = flag
            driver = SimpleNamespace(connect=Mock(return_value=object()))
            with patch.dict('sys.modules', psycopg2=driver), patch.dict(os.environ, PGOPTIONS='-c default_transaction_read_only=on'), \
                    patch('thunder_os.connectsafely_live.credential', return_value=lambda:'synthetic-dsn'), \
                    patch('thunder_os.legacy_writer_exclusion.verify_legacy_exclusion'), \
                    patch('thunder_os.connectsafely_live.direct_ledger_readiness', side_effect=lambda connect,identity: {'ready':bool(connect())}):
                self.assertTrue(self.configure().action_readiness('dm')['ready'])
                expected = {'connect_timeout':5}
                if flag is True: expected['options']='-c default_transaction_read_only=off'
                driver.connect.assert_called_once_with('synthetic-dsn', **expected)
                self.assertEqual('-c default_transaction_read_only=on', os.environ['PGOPTIONS'])
        self.cfg['live']['database_session_read_write'] = 'true'
        with self.assertRaisesRegex(ValueError, 'explicit_database_session_mode_required'):
            self.configure()

    def test_expiry_blocks_actions_at_boundary_and_restart_stays_disconnected(self):
        from thunder_os.connectsafely_console import ConnectSafelyError
        with patch('thunder_os.legacy_writer_exclusion.verify_legacy_exclusion'), \
                patch('thunder_os.connectsafely_live.direct_ledger_readiness', return_value={'ready':True}), \
                patch('thunder_os.connectsafely_live.time.time', return_value=4102444799) as clock:
            app = self.configure()
            for action in ('dm', 'invite'): self.assertTrue(app.action_readiness(action)['ready'])
            for action in ('comment','reply','react_post'): self.assertFalse(app.action_readiness(action)['ready'])
            clock.return_value = 4102444800
            for action in ('dm', 'invite'):
                self.assertFalse(app.action_readiness(action)['ready'])
                with self.assertRaises(ConnectSafelyError): app._enabled(action)
            self.assertFalse(app.status('ygor')['enqueue_enabled'])
            restarted = self.configure()
            self.assertIsNone(restarted.provider)
            self.assertFalse(restarted.status('ygor')['enqueue_enabled'])
            self.assertEqual([], self.repo.list_state('connectsafely_job'))

    def test_direct_target_resolution_uses_existing_read_port_and_expiry(self):
        with patch('thunder_os.connectsafely_read_targets.ReadTargetResolver') as resolver, \
                patch('thunder_os.legacy_writer_exclusion.verify_legacy_exclusion'), \
                patch('thunder_os.connectsafely_live.direct_ledger_readiness', return_value={'ready':True}), \
                patch('thunder_os.connectsafely_live.time.time', return_value=4102444799) as clock:
            resolver.return_value.return_value = {'resolved':True,'read_only':True,'fixture':True}
            app = self.configure()
            resolver.return_value.assert_not_called()
            body = {'action':'dm','profile_url':'https://www.linkedin.com/in/fixture-person/'}
            self.assertEqual({'resolved':True,'read_only':True,'fixture':True}, self.route(app, 'resolve-target', body)[1])
            resolver.return_value.assert_called_once_with(body)
            self.assertFalse(app.resolve_target({'action':'comment'})['resolved'])
            clock.return_value = 4102444800
            self.assertFalse(self.route(app, 'resolve-target', body)[1]['resolved'])
            resolver.return_value.assert_called_once_with(body)
            restarted = self.configure()
            self.assertFalse(self.route(restarted, 'resolve-target', body)[1]['resolved'])
            resolver.return_value.assert_called_once_with(body)
            self.assertEqual([], self.repo.list_state('connectsafely_job'))

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
                                         thunder_connectsafely_app=app,
                                         thunder_connectsafely_read_resolver=Mock(side_effect=AssertionError('independent resolver bypass')))
        handler.command = 'POST'
        handler.thunder_actor = lambda: actor
        handler.corpo = lambda: body
        handler.responder = lambda status, result: (status, result)
        with patch.object(panel, 'restricted_mode', return_value=False):
            return handler.thunder_api(urlsplit('/api/os/v1/integrations/connectsafely/' + operation))

    def invitation_attempt(self):
        app=self.configure()
        for target,kwargs in [(app.direct,{'_identity':None})]:
            for name,value in kwargs.items():
                guard=patch.object(target,name,return_value=value);guard.start();self.addCleanup(guard.stop)
        ready=patch.object(app,'action_readiness',return_value={'ready':True});ready.start();self.addCleanup(ready.stop)
        ledger=Mock();ledger.reserve.return_value=(True,{'id':'fixture-touch'})
        ledger.finish.return_value={'status':'uncertain','receipt_ref':None};app.direct.ledger=ledger
        self.invite_number=getattr(self,'invite_number',0)+1
        plan={'account_id':'a'*24,'account_public_id':'fixture-owner','action':'invite',
              'recipient_public_id':'fixture-target-'+str(self.invite_number),
              'recipient_profile_urn':'urn:li:fsd_profile:TARGET'+str(self.invite_number),
              'channel':'linkedin','messaging_channel':'linkedin_inbox','content':''}
        preview=app.prepare(plan,'ygor');review={'preview_id':preview['id'],'expected_version':preview['version'],'review_hash':preview['review_hash']}
        job=app.enqueue(review,'ygor')
        def uncertain(plan,before_dispatch,execution_context):
            before_dispatch();return {'status':'uncertain','error_code':'provider_outcome_unknown'}
        with patch.object(app.direct,'_send',side_effect=uncertain):app.process_one(job_id=job['job_id'])
        ledger.finish.reset_mock()
        result={'status':'sent','receipt_ref':'connectsafely:urn:li:invitation:123',
                'confirmation_kind':'observed_outbound','write_attribution_confirmed':False,
                'invitation_observation':{'account_id':'a'*24,'recipient_public_id':plan['recipient_public_id'],
                    'recipient_profile_urn':plan['recipient_profile_urn'],'relationship_status':'INVITATION_SENT',
                    'observed_at':app.clock(),'invitation_id':'123','invitation_urn':'urn:li:invitation:123',
                    'sent_at':app.clock(),'complete_history':True}}
        app.direct._reconcile=Mock(return_value=result)
        return app,preview,job,ledger,result

    def test_invite_reconcile_finishes_exact_job_and_audits_once_without_send(self):
        app,preview,job,ledger,result=self.invitation_attempt()
        first=self.route(app,'reconcile',{'preview_id':preview['id']})[1]
        self.assertEqual('sent',first['status']);self.assertFalse(first['write_attribution_confirmed'])
        final=self.repo.get_state('connectsafely_job',job['job_id'])
        self.assertEqual('done',final['state']);self.assertFalse(final['result']['retry_write'])
        self.assertEqual('INVITATION_SENT',final['result']['invitation_observation']['relationship_status'])
        self.assertEqual(1,len(self.repo.list_state('connectsafely_reconciliation')))
        self.route(app,'reconcile',{'preview_id':preview['id']})
        self.assertEqual(final,self.repo.get_state('connectsafely_job',job['job_id']))
        app.direct._reconcile.assert_called_once();ledger.reconcile_invitation.assert_called_once()
        self.assertEqual(1,len(self.repo.list_state('connectsafely_reconciliation')))

    def test_invite_observation_alone_keeps_job_uncertain_and_no_ledger_finish(self):
        app,preview,job,ledger,result=self.invitation_attempt()
        app.direct._reconcile.return_value={'status':'uncertain','error_code':'invite_receipt_unavailable',
                                            'invitation_observation':result['invitation_observation']}
        response=self.route(app,'reconcile',{'preview_id':preview['id']})[1]
        self.assertEqual('uncertain',response['status']);self.assertFalse(response['retry_write'])
        self.assertEqual('needs_reconciliation',self.repo.get_state('connectsafely_job',job['job_id'])['state'])
        ledger.finish.assert_not_called();ledger.reconcile_invitation.assert_not_called();self.assertEqual([],self.repo.list_state('connectsafely_reconciliation'))

    def test_invite_reconcile_rejects_wrong_approval_scope_time_and_attempt(self):
        from thunder_os.connectsafely_console import ConnectSafelyError
        for kind,field,value in [('connectsafely_queue_approval','actor','other'),
                                  ('connectsafely_queue_approval','expires_at',1),
                                  ('connectsafely_queue_approval','execution_scope','other'),
                                  ('connectsafely_job','state','running'),
                                  ('connectsafely_attempt','approval_hash','wrong'),
                                  ('connectsafely_attempt','attempt_token','other')]:
            with self.subTest(kind=kind,field=field):
                app,preview,job,ledger,result=self.invitation_attempt()
                saved=self.repo.get_state('connectsafely_preview',preview['id'])
                key=saved['effect_key'] if kind=='connectsafely_attempt' else job['job_id']
                row=self.repo.get_state(kind,key);self.repo.put_state(kind,key,{**row,field:value})
                with self.assertRaises(ConnectSafelyError):self.route(app,'reconcile',{'preview_id':preview['id']})
                app.direct._reconcile.assert_not_called();ledger.finish.assert_not_called();ledger.reconcile_invitation.assert_not_called()

    def test_invite_reconcile_rechecks_attempt_job_and_approval_after_read(self):
        for kind,field,value in [('connectsafely_attempt','attempt_token','new-token'),
                                  ('connectsafely_job','state','running'),
                                  ('connectsafely_queue_approval','actor','other')]:
            with self.subTest(kind=kind):
                app,preview,job,ledger,result=self.invitation_attempt()
                current=self.repo.get_state('connectsafely_preview',preview['id'])
                def race(plan,at):
                    key=current['effect_key'] if kind=='connectsafely_attempt' else job['job_id']
                    row=self.repo.get_state(kind,key);self.repo.put_state(kind,key,{**row,field:value})
                    return result
                app.direct._reconcile.side_effect=race
                with self.assertRaises(RepositoryConflict):self.route(app,'reconcile',{'preview_id':preview['id']})
                ledger.finish.assert_not_called();ledger.reconcile_invitation.assert_not_called()
                self.assertNotEqual('done',self.repo.get_state('connectsafely_job',job['job_id'])['state'])

    def test_invite_ledger_reconciliation_requires_exact_owned_mapping_and_is_terminal(self):
        import sqlite3
        from contextlib import contextmanager
        from thunder_os.connectsafely_ledger import ConnectSafelyLedger
        from thunder_os.touch_ledger import TouchBlocked
        path=self.root/'canonical-fixture.db'
        @contextmanager
        def database():
            con=sqlite3.connect(path)
            try:
                yield con,'?';con.commit()
            except Exception:
                con.rollback();raise
            finally:con.close()
        with database() as (con,_):
            con.execute('CREATE TABLE toques (id INTEGER PRIMARY KEY,canal TEXT,acao TEXT,pessoa_chave TEXT,alvo TEXT,status TEXT,provider_id TEXT,ref TEXT,detalhe TEXT,atualizado_em TEXT)')
            con.execute('CREATE TABLE thunder_effect_touches (effect_key TEXT PRIMARY KEY,touch_id INTEGER)')
            con.execute("INSERT INTO toques VALUES (1,'linkedin','convite','https://www.linkedin.com/in/fixture-person','convite','incerto',NULL,'preview','timeout',NULL)")
            con.execute("INSERT INTO thunder_effect_touches VALUES ('effect',1)")
        ledger=ConnectSafelyLedger(db_context=database)
        step={'channel':'linkedin','action':'invite','recipient':'https://www.linkedin.com/in/fixture-person','content':'','messaging_channel':'linkedin_inbox'}
        receipt='connectsafely:urn:li:invitation:123'
        for effect,touch,ref,plan in [('other',1,'preview',step),('effect',2,'preview',step),
                                      ('effect',1,'wrong',step),('effect',1,'preview',{**step,'recipient':'https://www.linkedin.com/in/other'}),
                                      ('effect',1,'preview',{**step,'action':'dm'})]:
            with self.assertRaises(TouchBlocked):ledger.reconcile_invitation(effect,touch,ref,plan,receipt)
        with database() as (con,_):
            self.assertEqual(('incerto',None),con.execute('SELECT status,provider_id FROM toques').fetchone())
        first=ledger.reconcile_invitation('effect',1,'preview',step,receipt)
        self.assertEqual('sent',first['status'])
        self.assertEqual(first,ledger.reconcile_invitation('effect',1,'preview',step,receipt))
        with self.assertRaises(TouchBlocked):ledger.reconcile_invitation('effect',1,'preview',step,'connectsafely:urn:li:invitation:456')
        with database() as (con,_):
            self.assertEqual(('feito',receipt),con.execute('SELECT status,provider_id FROM toques').fetchone())

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
