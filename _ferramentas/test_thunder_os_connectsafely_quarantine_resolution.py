"""Real PG16 tests on one task-owned Unix-socket cluster; no env DSN/provider.

Run using /private/tmp/thunder-social-reconcile/venv/bin/python, PYTHONPATH=_ferramentas.
The cluster is initialized separately; its data_directory identity is checked
before this suite replaces ONLY its dedicated social_reconcile_test database.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import psycopg2

from thunder_os.connectsafely_coordination import CoordinationError, WriterEvidence, InventoryEvidence, digest, encoded
from thunder_os.connectsafely_coordination_postgres import _PG, verify_schema
from thunder_os.connectsafely_backfill import dry_run, verify_backfill
from thunder_os.connectsafely_phase0 import apply_postgres_phase0, verify_postgres_phase0
from thunder_os.connectsafely_quarantine_resolution import plan_resolution
from thunder_os.connectsafely_quarantine_resolution_evidence import ReviewedReconciliation
from thunder_os.connectsafely_quarantine_resolution_postgres import (
    apply_postgres_resolution, verify_resolution_schema, verify_resolution_data,
    verify_claude_role, restore_global_guards, PostgresResolvedSocialCoordinator,
)

ROOT = Path('/private/tmp/thunder-social-reconcile')
DB = 'social_reconcile_test'
ROLE = 'thunder_claude_social'
SCHEMA = 'thunder_social_coordination'
ACCOUNT = 'a' * 24
PROFILE = 'https://www.linkedin.com/in/distinct-person'
FEED = 'https://www.linkedin.com/feed/update/urn:li:activity:'


def connect(role='social_test_admin', database=DB):
    return psycopg2.connect(host=str(ROOT), port=55439, dbname=database, user=role, connect_timeout=3,
                            options='-c statement_timeout=10000 -c lock_timeout=3000')


def artifact(name, data):
    path = ROOT / name
    path.write_bytes(encoded(data))
    return str(path), digest(path.read_bytes())


class ReconciliationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with connect(database='postgres') as con:
            cur = con.cursor()
            cur.execute('SHOW data_directory')
            if Path(cur.fetchone()[0]).resolve() != (ROOT / 'pgdata').resolve():
                raise RuntimeError('Refused: not the isolated task cluster')
            cur.execute('SELECT current_user,version()')
            role, version = cur.fetchone()
            if role != 'social_test_admin' or not version.startswith('PostgreSQL 16.'):
                raise RuntimeError('Refused: wrong test identity/version')
        con.close()

    def setUp(self):
        admin = connect(database='postgres')
        admin.autocommit = True
        with admin.cursor() as c:
            c.execute('DROP DATABASE IF EXISTS social_reconcile_test WITH (FORCE)')
            c.execute('CREATE DATABASE social_reconcile_test')
            for role in (ROLE, 'other_writer', 'stranger', 'os_reader'):
                c.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,))
                if not c.fetchone():
                    c.execute('CREATE ROLE ' + role + ' NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS')
            c.execute('ALTER ROLE thunder_claude_social NOLOGIN')
        admin.close()
        self.admin = connect()
        self.addCleanup(self.admin.close)
        with self.admin.cursor() as c:
            c.execute('CREATE TABLE public.toques(id bigserial PRIMARY KEY,criado_em timestamptz DEFAULT CURRENT_TIMESTAMP,atualizado_em timestamptz,canal text,acao text,pessoa_chave text,alvo text,status text,agente text,provider_id text,pessoa_nome text,conta text,ref text,detalhe text)')
            c.execute("CREATE UNIQUE INDEX toques_unico_ativo ON toques(canal,acao,pessoa_chave,alvo) WHERE status<>'liberado'")
            c.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
            records = [
                (7, 'alias-person', 'https://www.linkedin.com/posts/alias-person_first-activity-100-a'),
                (9, 'alias-person', 'https://www.linkedin.com/posts/alias-person_activity-100-a'),
                (29, 'inconclusive-person', 'https://www.linkedin.com/posts/inconclusive-person_original-activity-200-a'),
                (30, 'ugc-person', 'https://www.linkedin.com/posts/ugc-person_original-activity-400-a'),
                (173, 'group-person', 'https://www.linkedin.com/feed/update/urn:li:groupPost:9-300'),
            ]
            self.items = []
            for ident, slug, target in records:
                profile = 'https://www.linkedin.com/in/' + slug
                c.execute("INSERT INTO toques(id,canal,acao,pessoa_chave,alvo,status,agente) VALUES(%s,'linkedin','curtida',%s,%s,'feito','historic')", (ident, profile, target))
                post = 'urn:li:activity:' + ('100' if ident in (7, 9) else '400')
                if ident == 173:
                    post = target
                item = dict(touch_id=ident, profile=profile, original_target=target, family='curtida', historical_status='feito', canonical_target=post,
                            verdict='positive_provider_readback', provider_effect=dict(type='reaction', postUrl=target, activityUrn='999999', reactionType='LIKE'), alias_evidence=None)
                if ident == 29:
                    item.update(canonical_target=None, verdict='inconclusive', provider_effect=None)
                elif ident == 30:
                    item['provider_effect']['postUrl'] = 'https://www.linkedin.com/posts/ugc-person_other-ugcPost-401-a'
                    item['alias_evidence'] = dict(activityUrn=post, ugcPostUrn='urn:li:ugcPost:401')
                elif ident == 173:
                    item['provider_effect']['reactionType'] = None
                self.items.append(item)
            c.execute("SELECT setval(pg_get_serial_sequence('toques','id'),1000)")
        self.admin.commit()
        plan = dry_run(_PG(self.admin))
        self.history_pin = plan['manifest_hash']
        self.admin.rollback()
        apply_postgres_phase0(self.admin, reviewed_manifest_hash=self.history_pin, expected_social_count=5, provision_mapping=True)
        self.rows = [json.loads(r[0]) for r in self.query('SELECT row_json FROM thunder_social_coordination.history ORDER BY touch_id')]
        self.snapshot = self.query('SELECT * FROM toques ORDER BY id')
        self.review = self.make_review()
        source_path, source_pin = artifact('writer-fixture.json', {'synthetic': True})
        audit_path, audit_pin = artifact('audit-fixture.json', {'isolated': True})
        writer = WriterEvidence('haiku-connectsafely', source_path, source_pin, audit_path, audit_pin, database_role=ROLE)
        inventory_path, inventory_pin = artifact('inventory-fixture.json', {'version': 1, 'writers': [{'id': writer.writer_id, 'artifact_sha256': source_pin, 'audit_sha256': audit_pin, 'protocol': writer.protocol, 'database_role': ROLE}]})
        self.inventory = InventoryEvidence((writer,), (writer.writer_id,), inventory_path, inventory_pin)

    def query(self, sql, args=()):
        with self.admin.cursor() as c:
            c.execute(sql, args)
            result = c.fetchall() if c.description else None
        self.admin.commit()
        return result

    def make_review(self, scoped=(29,)):
        path, pin = artifact('provider-fixture.json', {'account': {'id': ACCOUNT}, 'items': self.items})
        review_path, review_pin = artifact('policy-fixture.json', dict(version=2, decision_ref='synthetic-test-approval', account_id=ACCOUNT, evidence_path=path, evidence_sha256=pin, scoped_inconclusive=list(scoped)))
        return ReviewedReconciliation(review_path, review_pin)

    def proposal(self):
        return plan_resolution(self.rows, self.review, expected_history_hash=digest(encoded(self.rows)))

    def install(self):
        result = apply_postgres_resolution(self.admin, self.review, history_manifest_hash=self.history_pin, reviewed_proposal_sha256=self.proposal()['proposal_sha256'], inventory=self.inventory)
        # Production migration deliberately does NOT enable LOGIN. Test connects
        # with this role as session_user, after explicit fixture-only activation.
        self.query('ALTER ROLE thunder_claude_social LOGIN')
        return result

    def runtime(self, role=ROLE):
        con = connect(role)
        self.addCleanup(con.close)
        return con

    def reserve(self, con, post='900', person=PROFILE, family='curtida'):
        with con.cursor() as c:
            c.execute('SELECT thunder_social_coordination.claude_reserve(%s,%s,%s,%s,%s,%s)', (family, person, FEED+post, 'Synthetic', 'Test', 'isolated-test'))
            return c.fetchone()[0]

    def test_history_and_immutability(self):
        self.assertTrue(self.install())
        self.assertEqual(self.snapshot, self.query('SELECT * FROM toques ORDER BY id'))
        self.assertEqual([(29,)], self.query('SELECT touch_id FROM thunder_social_coordination.scoped_exception'))
        self.assertEqual([], self.query('SELECT touch_id FROM thunder_social_coordination.resolution WHERE touch_id=29'))
        for sql in ("UPDATE toques SET status='erro' WHERE id=29", 'DELETE FROM toques WHERE id=7', 'UPDATE thunder_social_coordination.history SET row_json=row_json', 'DELETE FROM thunder_social_coordination.quarantine', 'UPDATE thunder_social_coordination.scoped_exception SET family=family', 'DELETE FROM thunder_social_coordination.resolution'):
            with self.assertRaises(psycopg2.Error):
                self.admin.cursor().execute(sql)
            self.admin.rollback()
        verify_backfill(_PG(self.admin), self.history_pin, allow_quarantine=True)
        self.admin.rollback()

    def test_scoped_inconclusive_and_distinct_post(self):
        self.install()
        con = self.runtime()
        for family in ('curtida', 'comentario'):
            self.assertIsNone(self.reserve(con, '200', family=family))
            for person in ('https://www.linkedin.com/in/inconclusive-person', 'http://linkedin.com/in/INCONCLUSIVE-PERSON/'):
                self.assertIsNone(self.reserve(con, '901', person, family))
        self.assertIsInstance(self.reserve(con), int)
        self.assertIsInstance(self.reserve(con, '902', 'https://www.linkedin.com/in/alias-person'), int)
        con.commit()
        self.assertEqual(2, self.query("SELECT count(*) FROM toques WHERE id>1000")[0][0])

    def test_unapproved_inconclusive_stays_global(self):
        self.review = self.make_review(scoped=())
        self.install()
        con = self.runtime()
        for family in ('curtida', 'comentario'):
            with self.assertRaisesRegex(psycopg2.Error, 'social_quarantine_unresolved'):
                self.reserve(con, family=family)
            con.rollback()

    def test_alias_and_group_fences(self):
        self.install()
        self.assertEqual([(7, 'urn:li:activity:100'), (9, 'urn:li:activity:100')], self.query('SELECT touch_id,post_urn FROM thunder_social_coordination.resolution WHERE touch_id IN (7,9) ORDER BY touch_id'))
        self.assertEqual([('urn:li:groupPost:9-300',)], self.query('SELECT post_urn FROM thunder_social_coordination.resolution WHERE touch_id=173'))
        self.assertEqual([(0,)], self.query('SELECT count(*) FROM thunder_social_coordination.claims'))
        con = self.runtime()
        for post in ('100', '400'):
            self.assertIsNone(self.reserve(con, post))
        for target in ('urn:li:groupPost:9-300','urn:li:ugcPost:401','urn:li:activity:900/'):
            with self.assertRaisesRegex(psycopg2.Error, 'social_target_unresolved'):
                con.cursor().execute('SELECT thunder_social_coordination.claude_reserve(%s,%s,%s,NULL,NULL,NULL)', ('curtida', PROFILE, 'https://www.linkedin.com/feed/update/'+target))
            con.rollback()
        self.assertIsInstance(self.reserve(con, '999999'), int)  # reaction event is not a post alias
        con.rollback()

    def test_writer_and_runtime_rollback(self):
        self.install()
        con = self.runtime()
        with con.cursor() as c:
            c.execute('SELECT session_user,current_user')
            self.assertEqual((ROLE, ROLE), c.fetchone())
        ident = self.reserve(con)
        with con.cursor() as c:
            c.execute('SELECT * FROM thunder_social_coordination.claude_readback(%s)', (ident,))
            self.assertEqual('reservado', c.fetchone()[4])
        con.rollback()
        self.assertEqual([(0,)], self.query('SELECT count(*) FROM toques WHERE id=%s', (ident,)))
        self.assertEqual([(0,)], self.query('SELECT count(*) FROM thunder_social_coordination.claims WHERE touch_id=%s', (ident,)))
        self.query('ALTER ROLE other_writer LOGIN')
        self.query('GRANT INSERT,SELECT ON toques TO other_writer')
        self.query('GRANT USAGE ON SEQUENCE toques_id_seq TO other_writer')
        self.query("INSERT INTO thunder_social_coordination.writers VALUES('another-writer','other_writer',%s,%s,%s)", ('a'*64,'b'*64,'c'*64))
        other = self.runtime('other_writer')
        with other.cursor() as c:
            c.execute("INSERT INTO toques(canal,acao,pessoa_chave,alvo,status,agente) VALUES('linkedin','curtida',%s,%s,'reservado','another-writer') RETURNING id", (PROFILE,FEED+'901'))
            self.assertIsInstance(c.fetchone()[0], int)
        other.commit()
        with self.assertRaisesRegex(psycopg2.Error, 'social_writer_unknown'):
            other.cursor().execute("INSERT INTO toques(canal,acao,pessoa_chave,alvo,status,agente) VALUES('linkedin','curtida',%s,%s,'reservado','haiku-connectsafely')", (PROFILE,FEED+'902'))
        other.rollback()
        with self.assertRaises(psycopg2.Error):
            self.reserve(other)
        other.rollback()
        for family in ('curtida','comentario'):
            for person,post in ((PROFILE,'200'),('https://www.linkedin.com/in/inconclusive-person','903')):
                with self.assertRaisesRegex(psycopg2.Error,'social_quarantine_unresolved'):
                    other.cursor().execute("INSERT INTO toques(canal,acao,pessoa_chave,alvo,status,agente) VALUES('linkedin',%s,%s,%s,'reservado','another-writer')", (family,person,FEED+post))
                other.rollback()

    def test_complete_and_readback_restrictions(self):
        self.install()
        con = self.runtime()
        ident = self.reserve(con)
        con.commit()
        with self.assertRaisesRegex(psycopg2.Error, 'social_completion_invalid'):
            con.cursor().execute('SELECT thunder_social_coordination.claude_complete(%s,%s,NULL,NULL)', (ident,'feito'))
        con.rollback()
        con.cursor().execute('SELECT thunder_social_coordination.claude_complete(%s,%s,NULL,%s)', (ident,'feito','provider readback confirmed; no receipt ID returned'))
        con.commit()
        self.assertEqual([('feito',None)], self.query('SELECT status,provider_id FROM toques WHERE id=%s', (ident,)))
        for touch, status in ((ident,'erro'),(29,'feito'),(999999,'feito')):
            with self.assertRaisesRegex(psycopg2.Error, 'social_reservation_unverified'):
                con.cursor().execute('SELECT thunder_social_coordination.claude_complete(%s,%s,%s,NULL)', (touch,status,'evidence'))
            con.rollback()
        for status in ('erro','incerto'):
            touch = self.reserve(con, '91'+str(len(status)))
            con.cursor().execute('SELECT thunder_social_coordination.claude_complete(%s,%s,NULL,NULL)', (touch,status))
            con.commit()
        with con.cursor() as c:
            c.execute('SELECT * FROM thunder_social_coordination.claude_readback(29)')
            self.assertEqual([], c.fetchall())
        con.rollback()
        with self.assertRaisesRegex(psycopg2.Error,'social_person_invalid'):
            self.reserve(con, person=FEED+'900')
        con.rollback()
        with self.assertRaisesRegex(psycopg2.Error,'social_person_invalid'):
            self.reserve(con,person='https://www.linkedin.com/in/%69nconclusive-person')
        con.rollback()
        touch = self.reserve(con,'950')
        con.cursor().execute('SELECT thunder_social_coordination.claude_complete(%s,%s,%s,NULL)',(touch,'feito','synthetic-receipt'))
        con.commit()
        self.assertEqual([('feito','synthetic-receipt')],self.query('SELECT status,provider_id FROM toques WHERE id=%s',(touch,)))

    def test_concurrent_claim(self):
        self.install()
        first = self.runtime()
        ident = self.reserve(first)
        def second():
            con = connect(ROLE)
            try:
                result = self.reserve(con, person='https://www.linkedin.com/in/another-person')
                con.commit()
                return result
            finally:
                con.close()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(second)
            time.sleep(.15)
            self.assertFalse(future.done())
            first.commit()
            self.assertIsNone(future.result(timeout=5))
        self.assertEqual([(ident,)], self.query("SELECT touch_id FROM thunder_social_coordination.claims WHERE post_urn='urn:li:activity:900'"))
        self.assertEqual([(1,)], self.query('SELECT count(*) FROM toques WHERE id>1000'))

    def test_migration_transaction_and_recovery(self):
        with patch('thunder_os.connectsafely_quarantine_resolution_postgres.verify_resolution_data', side_effect=CoordinationError('injected_failure')):
            with self.assertRaisesRegex(CoordinationError,'injected_failure'):
                self.install()
        self.assertEqual([(None,)], self.query("SELECT to_regclass('thunder_social_coordination.resolution')"))
        self.assertEqual(self.snapshot, self.query('SELECT * FROM toques ORDER BY id'))
        verify_schema(_PG(self.admin))
        self.admin.rollback()
        self.assertTrue(self.install())
        self.assertFalse(self.install())
        con = self.runtime()
        ident = self.reserve(con)
        con.commit()
        restore_global_guards(self.admin)
        with self.assertRaisesRegex(psycopg2.Error,'social_quarantine_unresolved'):
            self.reserve(con, '902')
        con.rollback()
        self.assertEqual([(ident,)], self.query('SELECT touch_id FROM thunder_social_coordination.claims'))
        self.assertEqual([(1,)], self.query('SELECT count(*) FROM thunder_social_coordination.scoped_exception'))
        with self.assertRaises(CoordinationError):
            verify_resolution_schema(_PG(self.admin))
        self.admin.rollback()

    def test_acl_and_function_pins(self):
        self.install()
        verify_resolution_schema(_PG(self.admin))
        self.assertEqual(0, verify_resolution_data(_PG(self.admin)))
        verify_claude_role(_PG(self.admin))
        self.admin.rollback()
        with self.assertRaises(CoordinationError):
            verify_postgres_phase0(self.admin, reviewed_manifest_hash=self.history_pin, expected_social_count=5)
        con = self.runtime()
        for sql in ('SELECT * FROM toques','UPDATE toques SET status=\'feito\'','DELETE FROM toques','INSERT INTO thunder_social_coordination.resolution VALUES(1,\'curtida\',\'urn:li:activity:1\',\'x\')','CREATE TABLE public.bad(id int)',"SELECT thunder_social_coordination.issue(NULL,NULL,NULL,NULL,NULL,NULL,NULL,1)","SELECT thunder_social_coordination.consume(NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL)"):
            with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
                con.cursor().execute(sql)
            con.rollback()
        self.admin.cursor().execute('ALTER TABLE public.toques DISABLE TRIGGER thunder_social_guard')
        with self.assertRaisesRegex(CoordinationError,'social_schema_changed'):
            verify_resolution_schema(_PG(self.admin))
        self.admin.rollback()

    def test_evidence_validation(self):
        for mutate, error in (
            (lambda: self.items[0].update(profile='https://www.linkedin.com/in/wrong-person'), 'social_resolution_person_mismatch'),
            (lambda: self.items[0]['provider_effect'].update(type='comment'), 'social_resolution_evidence_invalid'),
            (lambda: self.items[3].update(alias_evidence=None), 'social_resolution_alias_unproven'),
        ):
            original = json.loads(json.dumps(self.items))
            mutate()
            self.review = self.make_review()
            with self.assertRaisesRegex(CoordinationError,error):
                self.proposal()
            self.items = original
        self.review = self.make_review()
        policy = json.loads(Path(self.review.path).read_text())
        policy['account_id'] = 'b'*24
        self.review = ReviewedReconciliation(*artifact('policy-fixture.json', policy))
        with self.assertRaisesRegex(CoordinationError,'social_resolution_account_mismatch'):
            self.proposal()

    def test_postgresql_adapter_and_capability(self):
        self.install()
        self.query('ALTER ROLE os_reader LOGIN')
        self.query('GRANT USAGE ON SCHEMA thunder_social_coordination TO os_reader')
        self.query('GRANT SELECT ON ALL TABLES IN SCHEMA thunder_social_coordination TO os_reader')
        self.query('GRANT SELECT ON public.toques,public.thunder_effect_touches TO os_reader')
        # Phase0 does not grant capability functions. This fixture's separate
        # OS reader deliberately enables them; Claude keeps its three functions.
        self.query('GRANT EXECUTE ON FUNCTION thunder_social_coordination.issue(text,text,text,text,text,text,text,integer),thunder_social_coordination.consume(text,text,text,text,text,text,double precision,text),thunder_social_coordination.retire(text,text,text) TO os_reader')
        con = self.runtime()
        touch = self.reserve(con)
        con.commit()
        self.query('INSERT INTO thunder_effect_touches VALUES(%s,%s)', ('d'*64,touch))
        coordinator = PostgresResolvedSocialCoordinator(lambda:connect('os_reader'), self.inventory, backfill_manifest_hash=self.history_pin)
        self.assertTrue(coordinator.readiness()['verified'])
        cap = coordinator.issue('d'*64, account_id=ACCOUNT, action='react_post', post_urn='urn:li:activity:900', attempt_token='attempt-one', request_hash='f'*64)
        self.assertTrue(cap.validate(action='react_post',account_id=ACCOUNT,post_urn='urn:li:activity:900',request_hash='f'*64))
        self.assertTrue(cap.require(action='react_post',account_id=ACCOUNT,post_urn='urn:li:activity:900',request_hash='f'*64))
        with self.assertRaises(CoordinationError):
            cap.require(action='react_post',account_id=ACCOUNT,post_urn='urn:li:activity:900',request_hash='f'*64)
        self.assertEqual([('consumed',)],self.query('SELECT state FROM thunder_social_coordination.attempts'))

    def test_evidence_drift_and_role_drift_fail_closed(self):
        self.install()
        path=Path(self.review.path)
        raw=path.read_bytes()
        path.write_bytes(raw+b' ')
        with self.assertRaisesRegex(CoordinationError,'social_mapping_source_changed'):
            verify_resolution_data(_PG(self.admin))
        self.admin.rollback()
        path.write_bytes(raw)
        self.admin.cursor().execute('GRANT UPDATE(status) ON toques TO thunder_claude_social')
        with self.assertRaisesRegex(CoordinationError,'social_claude_overprivileged'):
            verify_claude_role(_PG(self.admin))
        self.admin.rollback()


if __name__ == '__main__':
    unittest.main()
