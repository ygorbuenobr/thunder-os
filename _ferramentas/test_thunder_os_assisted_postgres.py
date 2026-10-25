"""Opt-in real PostgreSQL composition test; never a production/provider test.

Run only with THUNDER_TEST_ASSISTED_PG=1 against the pre-existing disposable
cluster /tmp/thunder-audit-pg.wDcTXE, private Unix socket, port 55439, no TCP.
Requires an already installed psycopg2. No DSN/env credentials are accepted.
The suite provisions/drops only uniquely named synthetic roles and schema.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from email.utils import format_datetime
import hashlib
import json
import os
from pathlib import Path
import pwd
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

try:
    import psycopg2
    from psycopg2 import sql
except ImportError:
    psycopg2 = None

from thunder_os.approvals import _step_hash
from thunder_os.codex_assisted import configured_assisted
from thunder_os.db import Repository
from thunder_os.email_readback import GmailReceiptReader
from thunder_os.models import payload_hash
from thunder_os.seqd_client import MAILBOX
from thunder_os.service import ThunderService
from thunder_os.touch_ledger import AssistedLedger, TouchBlocked, TouchLedger
import test_thunder_os_thunder_email_runtime as email_fixtures

SOCKET = Path('/tmp/thunder-audit-pg.wDcTXE')
PORT = 55439


@unittest.skipUnless(os.environ.get('THUNDER_TEST_ASSISTED_PG') == '1' and psycopg2,
                     'NOT RUN: opt-in disposable PostgreSQL/installed psycopg2 required')
class AssistedPostgresTests(unittest.TestCase):
    @classmethod
    def connect(cls, role=None):
        # Every connection field identifying a destination is fixed here. libpq
        # cannot choose an environment DSN, hostname, port or production password.
        return psycopg2.connect(host=str(SOCKET), port=PORT, dbname='postgres',
            user=role or pwd.getpwuid(os.getuid()).pw_name, password='',
            passfile='/dev/null', connect_timeout=3,
            options='-c search_path=' + cls.schema + ' -c statement_timeout=5000')

    @classmethod
    def setUpClass(cls):
        suffix = uuid4().hex[:12]
        cls.schema, cls.ro, cls.rw = ('s38_' + suffix, 's38_ro_' + suffix, 's38_rw_' + suffix)
        with closing(cls.connect()) as con, con:
            with con.cursor() as cur:
                cur.execute('SHOW data_directory')
                if Path(cur.fetchone()[0]).resolve() != (SOCKET / 'data').resolve():
                    raise RuntimeError('disposable PostgreSQL identity mismatch')
                cur.execute('SHOW listen_addresses')
                if cur.fetchone()[0] != '':
                    raise RuntimeError('test cluster must have TCP disabled')
        cls.addClassCleanup(cls.drop_fixture)
        with closing(cls.connect()) as con, con:
            with con.cursor() as cur:
                for role in (cls.ro, cls.rw):
                    cur.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE').format(sql.Identifier(role)))
                cur.execute(sql.SQL('ALTER ROLE {} SET default_transaction_read_only=on').format(sql.Identifier(cls.ro)))
                cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(cls.schema)))
                cur.execute('''CREATE TABLE toques(id bigserial PRIMARY KEY,canal text,acao text,
                    pessoa_chave text,alvo text,status text,agente text,ref text,provider_id text,
                    detalhe text,atualizado_em timestamptz DEFAULT now());
                    CREATE TABLE respostas(id bigserial PRIMARY KEY,canal text,pessoa_chave text);
                    CREATE TABLE thunder_suppressions(canal text,pessoa_chave text,reason text,
                    PRIMARY KEY(canal,pessoa_chave));
                    CREATE TABLE thunder_effect_touches(effect_key text PRIMARY KEY,
                    touch_id bigint REFERENCES toques(id));''')
                for role in (cls.ro, cls.rw):
                    cur.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(cls.schema), sql.Identifier(role)))
                    cur.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(cls.schema), sql.Identifier(role)))
                cur.execute(sql.SQL('GRANT INSERT,UPDATE ON toques TO {}').format(sql.Identifier(cls.rw)))
                cur.execute(sql.SQL('GRANT INSERT ON thunder_effect_touches TO {}').format(sql.Identifier(cls.rw)))
                cur.execute(sql.SQL('GRANT USAGE ON SEQUENCE toques_id_seq TO {}').format(sql.Identifier(cls.rw)))

    @classmethod
    def drop_fixture(cls):
        con = cls.connect()
        try:
            with con:
                with con.cursor() as cur:
                    cur.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(cls.schema)))
                    for role in (cls.ro, cls.rw):
                        cur.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))
        finally:
            con.close()

    def context(self, role):
        @contextmanager
        def database():
            con = self.connect(role)
            try:
                with con:
                    if role == self.ro:
                        con.set_session(readonly=True)
                    yield con, '%s'
            finally:
                con.close()
        return database

    def setUp(self):
        con = self.connect()
        try:
            with con:
                with con.cursor() as cur:
                    cur.execute('TRUNCATE thunder_effect_touches,toques,respostas,thunder_suppressions RESTART IDENTITY')
        finally:
            con.close()
        self.reader = TouchLedger(db_context=self.context(self.ro))
        self.writer = TouchLedger(db_context=self.context(self.rw))
        self.ledger = AssistedLedger(self.reader, self.writer)
        self.step = {'id':'e1','channel':'email','recipient':'fixture@example.invalid',
            'subject':'Synthetic PG composition','content':'Synthetic, never send',
            'order':1,'interval_hours':0}
        # Providers are always doubles. Unix-domain DBAPI traffic does not use
        # Python HTTP clients; attempts to reach provider HTTP fail the test.
        for name in ('http.client.HTTPConnection', 'http.client.HTTPSConnection'):
            guard = patch(name, side_effect=AssertionError('provider HTTP forbidden'))
            guard.start();self.addCleanup(guard.stop)

    def count(self):
        with self.context(self.ro)() as (con, _):
            with con.cursor() as cur:
                cur.execute('SELECT count(*) FROM toques')
                return cur.fetchone()[0]

    def test_actual_dbapi_reader_and_writer_reserve_confirm_readback(self):
        with self.context(self.ro)() as (con, _):
            with con.cursor() as cur:
                cur.execute("SELECT current_user,current_setting('transaction_read_only'),has_table_privilege(current_user,'toques','INSERT')")
                self.assertEqual(cur.fetchone(), (self.ro, 'on', False))
        self.assertIsNone(self.ledger.preflight('effect-a', self.step)['duplicate'])
        created, row = self.ledger.reserve('effect-a', 'e1', self.step, strict_new=True)
        self.assertTrue(created);self.assertEqual(row['status'], 'reserved')
        self.assertEqual(self.ledger.reservation('effect-a', self.step)['id'], row['id'])
        confirmed = self.ledger.confirm_receipt('effect-a', self.step, 'gmail:abc123')
        self.assertEqual(confirmed['status'], 'sent')
        self.assertEqual(self.ledger.preflight('effect-a', self.step)['duplicate']['receipt_ref'], 'gmail:abc123')
        self.assertEqual(self.ledger.confirm_receipt('effect-a', self.step, 'gmail:abc123'), confirmed)
        self.assertEqual(self.count(), 1)

    def test_readonly_role_and_missing_writer_cannot_mutate(self):
        with self.assertRaises(psycopg2.Error):
            self.reader.reserve('reader-write', 'e1', self.step)
        without_writer = AssistedLedger(self.reader)
        with self.assertRaisesRegex(TouchBlocked, 'assisted_writer_not_configured'):
            without_writer.reserve('missing-writer', 'e1', self.step)
        with self.assertRaisesRegex(TouchBlocked, 'assisted_writer_not_configured'):
            without_writer.confirm_receipt('missing-writer', self.step, 'gmail:abc123')
        with self.assertRaisesRegex(ValueError, 'explicit_schema_provisioning_required'):
            self.writer.migrate()
        self.assertEqual(self.count(), 0)

    def test_wrong_receipt_or_scope_never_changes_real_reservation(self):
        self.ledger.reserve('effect-a', 'e1', self.step, strict_new=True)
        for changed in ({**self.step,'recipient':'other@example.invalid'}, {**self.step,'id':'other-step'}):
            with self.assertRaises(TouchBlocked):
                self.ledger.confirm_receipt('effect-a', changed, 'gmail:abc123')
        self.ledger.confirm_receipt('effect-a', self.step, 'gmail:abc123')
        with self.assertRaises(TouchBlocked):
            self.ledger.confirm_receipt('effect-a', self.step, 'gmail:fff999')
        self.assertEqual(self.ledger.reservation('effect-a', self.step)['receipt_ref'], 'gmail:abc123')
        self.assertEqual(self.count(), 1)

    def test_postgres_person_lock_serializes_duplicate_reservations(self):
        def reserve(number):
            ledger = AssistedLedger(self.reader, TouchLedger(db_context=self.context(self.rw)))
            return ledger.reserve('concurrent-' + str(number), 'e1', self.step)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
        self.assertEqual(sum(created for created, _ in results), 1)
        self.assertEqual({row['id'] for _, row in results}, {'1'})
        self.assertEqual(self.count(), 1)

    def configured_fixture(self, writer=True):
        temporary = tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        repo = Repository(Path(temporary.name) / 'workflow.db')
        base = ThunderService(repo, self.reader)
        ident = 'internal-canary-postgres-fixture'
        scope = {'kind':'internal_canary','from':MAILBOX,'to':self.step['recipient'],
            'subject':self.step['subject'],'content':self.step['content'],'max_attempts':1,'no_followup':True}
        now = int(time.time())
        policy = {'version':1,'approval_ref':'synthetic-pg-only','mailbox':MAILBOX,
            'campaign_id':ident,'cadence_id':ident,'step_id':'e1','effect_key':_step_hash(self.step),
            'recipient':scope['to'],'subject':scope['subject'],'content_hash':payload_hash(scope['content']),
            'issued_at':now-1,'expires_at':now+600,'max_attempts':1}
        raw = json.dumps(policy).encode();path = Path(temporary.name) / 'synthetic-policy.json';path.write_bytes(raw)
        env = patch.dict(os.environ, {'THUNDER_OS_CODEX_ASSISTED_DISPATCH':'true',
            'THUNDER_EXTERNAL_EFFECTS_ENABLED':'true','THUNDER_OS_CODEX_ASSISTED_POLICY_FILE':str(path),
            'THUNDER_OS_CODEX_ASSISTED_POLICY_SHA256':hashlib.sha256(raw).hexdigest()})
        env.start();self.addCleanup(env.stop)
        app = configured_assisted(base, ledger_reader=self.reader, ledger_writer=self.writer if writer else None)
        mail = email_fixtures.Mail();mail.envelope = {'to':scope['to'],'subject':scope['subject'],
            'body':scope['content'],'thread_id':None};mail.date = format_datetime(datetime.fromtimestamp(now, timezone.utc))
        app.reader = GmailReceiptReader(repo, mail)
        prepared = app.service.prepare({'id':ident,'scope':scope,'approval_ref':policy['approval_ref']}, 'ygor')
        cadence = prepared['cadence']
        app.service.approve_cadence(ident,'ygor',expected_version=cadence['version'],
            review_hash=prepared['review_hash'],execution_confirmation=prepared['execution_confirmation'])
        job = app.service.dispatch_step(ident,'e1','ygor')
        return app, repo, mail, job, ident

    def test_configured_entrypoint_complete_claim_dispatch_receipt_with_real_pg(self):
        app, repo, mail, job, ident = self.configured_fixture()
        packet = app.preview(ident, 'e1')
        ticket = app.claim(ident, 'e1', packet['review_hash'])
        self.assertEqual(self.count(), 1)
        self.assertEqual(repo.effect(job['id'])['state'], 'uncertain')
        app.dispatch(job['id'], ticket['ticket_hash'])
        preview = app.receipt_preview(job['id'], 'gmail:abc123')
        app.receipt_confirm(job['id'], 'gmail:abc123', preview['review_hash'])
        self.assertEqual(repo.effect(job['id'])['state'], 'sent')
        self.assertEqual(self.ledger.reservation(job['effect_key'], self.step)['receipt_ref'], 'gmail:abc123')
        self.assertEqual(mail.sends, [])
        self.assertEqual(repo.get_state('codex_assisted',job['id'])['verification'], 'provider_sent_copy')

    def test_pg_commit_then_local_failure_requires_explicit_reconciliation(self):
        app, repo, mail, job, ident = self.configured_fixture()
        packet = app.preview(ident, 'e1')
        with patch.object(repo, 'finish_effect', side_effect=RuntimeError('synthetic local failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic local failure'):
                app.claim(ident, 'e1', packet['review_hash'])
        self.assertEqual(self.count(), 1)
        self.assertIsNotNone(repo.get_state('assisted_reservation_intent', job['id']))
        self.assertEqual(repo.effect(job['id'])['state'], 'pending')
        self.assertIsNone(repo.get_state('codex_assisted', job['id']))
        with self.assertRaisesRegex(ValueError, 'assisted_reservation_reconciliation_required'):
            app.claim(ident, 'e1', packet['review_hash'])
        ticket = app.reconcile_reservation(ident, 'e1', packet['review_hash'])
        self.assertEqual(ticket['reservation_id'], '1')
        self.assertEqual(self.count(), 1)
        self.assertEqual(repo.effect(job['id'])['state'], 'uncertain')
        self.assertEqual(mail.sends, [])

    def test_strict_reservation_cannot_adopt_another_effect(self):
        self.writer.reserve('original-effect', 'e1', self.step, strict_new=True)
        created, _ = self.writer.reserve('other-effect', 'e1', self.step, strict_new=True)
        self.assertFalse(created)
        self.assertIsNone(self.reader.reservation('other-effect', self.step))
        self.assertEqual(self.count(), 1)

    def test_configured_entrypoint_missing_writer_has_no_fallback(self):
        app, repo, mail, job, ident = self.configured_fixture(writer=False)
        packet = app.preview(ident, 'e1')
        with self.assertRaisesRegex(TouchBlocked, 'assisted_writer_not_configured'):
            app.claim(ident, 'e1', packet['review_hash'])
        self.assertEqual(self.count(), 0)
        self.assertEqual(repo.effect(job['id'])['state'], 'pending')
        self.assertIsNone(repo.get_state('codex_assisted', job['id']))
        self.assertEqual(mail.sends, [])


if __name__ == '__main__': unittest.main()
