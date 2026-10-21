"""Synthetic runner/receipt tests with the actual pinned toques dependency."""
from contextlib import closing, contextmanager
from copy import deepcopy
from html import escape
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from thunder_os.mexico_receipt import MARKER, receipt_key
from thunder_os.cadence_readonly import project_task
from test_thunder_os_cadence_evidence import mexico, STAMP, HASH, BATCH

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('mexico_bridge', ROOT / 'scripts/mexico_toques_bridge.py')
BRIDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE)
DEPENDENCY = Path(os.environ.get('THUNDER_TEST_TOQUES_PATH', ROOT / '.scratch/dependencies/toques.py'))


def create_fixture(path):
    # Existing canonical table/unique key, only in a test-owned SQLite file.
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript('''CREATE TABLE toques (
            id INTEGER PRIMARY KEY AUTOINCREMENT, criado_em TEXT DEFAULT CURRENT_TIMESTAMP,
            atualizado_em TEXT, canal TEXT, acao TEXT, pessoa_chave TEXT, pessoa_nome TEXT,
            conta TEXT, alvo TEXT, status TEXT, agente TEXT, ref TEXT, provider_id TEXT,
            detalhe TEXT, angulo TEXT, variante TEXT);
            CREATE UNIQUE INDEX toques_unico_ativo ON toques(canal,acao,pessoa_chave,alvo)
            WHERE status <> 'liberado';''')


def synthetic_binding(touch='E1', task='1'):
    return {'campaign_id': 'MX27-CDMX', 'batch': BATCH, 'hubspot_task_id': task,
        'contact_id': '101', 'touch': touch, 'sender': 'ygor.bueno@thundermustard.com',
        'recipient': 'synthetic@example.test', 'payload_sha256': HASH}


def synthetic_receipt(b):
    return {**b, 'marker': MARKER, 'key': receipt_key(b), 'provider': 'gmail',
        'verification': 'full_readback', 'state': 'PROVIDER_CONFIRMED',
        'message_id': 'abc123', 'confirmed_at': STAMP}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'touches.db'
        create_fixture(self.db)
        self.module = BRIDGE.load_dependency(DEPENDENCY)

        @contextmanager
        def banco():
            conn = sqlite3.connect(self.db, timeout=5)
            try:
                yield conn, '?'
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        self.module._banco = banco
        self.adapter = BRIDGE.MexicoTouches(self.module)
        self.b = synthetic_binding()
        self.request = {'binding': self.b, 'subject': 'Same thread subject'}
        self.row = self.adapter.apply({**self.request, 'op': 'reserve'})

    def confirm(self, receipt=None):
        return self.adapter.apply({**self.request, 'op': 'confirm', 'touch_id': self.row['touch_id'],
            'receipt': receipt or synthetic_receipt(self.b)})

    def test_real_reservation_metadata_roundtrip_no_silently_lost_task_kwarg(self):
        self.assertTrue(self.row['created'])
        result = self.confirm()
        with closing(sqlite3.connect(self.db)) as conn:
            status, ref, detail, provider = conn.execute('SELECT status,ref,detalhe,provider_id FROM toques').fetchone()
        self.assertEqual(status, 'feito')
        self.assertEqual(json.loads(ref)['hubspot_task_id'], '1')
        self.assertEqual(json.loads(ref)['campaign_id'], 'MX27-CDMX')
        self.assertEqual(json.loads(detail)['receipt'], result['receipt'])
        self.assertEqual(provider, 'abc123')

    def test_confirmed_is_terminal_and_different_receipt_refused(self):
        first = self.confirm()
        later = self.adapter.apply({**self.request, 'op': 'uncertain', 'touch_id': self.row['touch_id']})
        self.assertEqual(later, first)
        changed = synthetic_receipt(self.b)
        changed['message_id'] = 'def456'
        with self.assertRaisesRegex(ValueError, 'TERMINAL_RECEIPT_MISMATCH'):
            self.confirm(changed)
        self.assertEqual(self.adapter.apply({**self.request, 'op': 'lookup'})['receipt'], first['receipt'])

    def test_interleaved_late_uncertain_cannot_overwrite_confirmed(self):
        read = threading.Event()
        resume = threading.Event()
        original = self.adapter._finish
        def interleaved(identifier, status, detail, provider_id=None):
            if status == 'incerto':
                read.set()
                self.assertTrue(resume.wait(5))
            return original(identifier, status, detail, provider_id)
        self.adapter._finish = interleaved
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.adapter.apply, {**self.request, 'op': 'uncertain', 'touch_id': self.row['touch_id']})
            self.assertTrue(read.wait(5))
            receipt = self.confirm()['receipt']
            resume.set()
            self.assertEqual(future.result()['receipt'], receipt)
        with closing(sqlite3.connect(self.db)) as conn:
            row = conn.execute('SELECT status,provider_id,detalhe FROM toques').fetchone()
        self.assertEqual(row[:2], ('feito', 'abc123'))
        self.assertEqual(json.loads(row[2])['receipt'], receipt)

    def test_concurrent_confirmation_cannot_replace_other_receipt(self):
        read, resume = threading.Event(), threading.Event()
        original = self.adapter._finish
        def interleaved(identifier, status, detail, provider_id=None):
            if provider_id == 'def456':
                read.set()
                self.assertTrue(resume.wait(5))
            return original(identifier, status, detail, provider_id)
        self.adapter._finish = interleaved
        other = synthetic_receipt(self.b)
        other['message_id'] = 'def456'
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.confirm, other)
            self.assertTrue(read.wait(5))
            self.confirm()
            resume.set()
            with self.assertRaisesRegex(ValueError, 'TERMINAL_RECEIPT_MISMATCH'):
                future.result()

    def test_same_subject_later_step_preserves_history_and_dedups_per_step(self):
        later = {**self.request, 'binding': synthetic_binding('E2', '2')}
        with self.assertRaisesRegex(ValueError, 'PREVIOUS_TOUCH_UNVERIFIED'):
            self.adapter.apply({**later, 'op': 'reserve'})
        self.confirm()
        e2 = self.adapter.apply({**later, 'op': 'reserve'})
        self.assertTrue(e2['created'])
        self.assertNotEqual(e2['touch_id'], self.row['touch_id'])
        self.assertFalse(self.adapter.apply({**later, 'op': 'reserve'})['created'])
        self.assertEqual(self.adapter.apply({**self.request, 'op': 'lookup'})['status'], 'feito')
        mismatch = deepcopy(later)
        mismatch['binding']['campaign_id'] = 'OTHER'
        with self.assertRaisesRegex(ValueError, 'PREVIOUS_TOUCH_UNVERIFIED'):
            self.adapter.apply({**mismatch, 'op': 'reserve'})

    def test_legacy_unlinked_history_blocks(self):
        self.module.reservar('email', 'email', 'legacy@example.test', 'Legacy subject', ref='old ledger')
        for touch in ['E1', 'E2']:
            b = {**self.b, 'recipient': 'legacy@example.test', 'touch': touch}
            with self.assertRaisesRegex(ValueError, 'HISTORY_BINDING_MISMATCH|PREVIOUS_TOUCH_UNVERIFIED'):
                self.adapter.apply({'op': 'reserve', 'binding': b, 'subject': 'Legacy subject'})

    def test_hash_pin_rejects_new_brain_version(self):
        path = Path(self.tmp.name) / 'wrong.py'
        path.write_text('# synthetic wrong dependency\n')
        with self.assertRaisesRegex(ValueError, 'TOQUES_DEPENDENCY_MISMATCH'):
            BRIDGE.load_dependency(path)


class StructuredReaderTests(unittest.TestCase):
    def structured(self, touch='E1'):
        row = mexico(outcome='none')
        row['properties']['hs_task_subject'] = row['properties']['hs_task_subject'].replace('| E1 |', f'| {touch} |')
        row['properties']['hs_task_body'] = row['properties']['hs_task_body'].replace(':E1"', f':{touch}"')
        receipt = synthetic_receipt(synthetic_binding(touch))
        row['properties']['hs_task_body'] += '<pre>' + escape(json.dumps(receipt)) + '</pre>'
        return row

    def project(self, row, contacts=None, complete=True):
        return project_task(row, associated_contact_ids=contacts if contacts is not None else ['101'], association_complete=complete)

    def test_structured_e1_and_e2_do_not_borrow_legacy_e1(self):
        for touch in ['E1', 'E2', 'E3']:
            result = self.project(self.structured(touch))
            self.assertEqual(result['outcome'], 'sent_with_receipt')
            self.assertEqual(result['receipt']['touch'], touch)
            self.assertEqual(result['receipt']['verification'], 'recorded_confirmation')
        row = self.structured('E2')
        row['properties']['hs_task_body'] = row['properties']['hs_task_body'].replace(
            escape('"touch": "E2"'), escape('"touch": "E1"'))
        self.assertIsNone(self.project(row)['receipt'])
        legacy = mexico()
        legacy['properties']['hs_task_subject'] = legacy['properties']['hs_task_subject'].replace('| E1 |', '| E2 |')
        self.assertIsNone(self.project(legacy)['receipt'])

    def test_mismatches_unlinked_wrong_mailbox_and_incomplete_are_unverified(self):
        for field, value in [('hubspot_task_id', '2'), ('campaign_id', 'OTHER'), ('contact_id', '102'),
                             ('payload_sha256', 'b' * 64), ('sender', 'other@example.test'), ('touch', 'E2')]:
            row = self.structured()
            before = synthetic_receipt(synthetic_binding())
            after = {**before, field: value}
            row['properties']['hs_task_body'] = row['properties']['hs_task_body'].replace(
                escape(json.dumps(before)), escape(json.dumps(after)))
            self.assertIsNone(self.project(row)['receipt'], field)
        for contacts, complete in [([], True), (['102'], True), (['101'], False), (['101', '102'], True)]:
            self.assertIsNone(self.project(self.structured(), contacts, complete)['receipt'])
        row = self.structured()
        row['properties']['hs_task_status'] = 'WAITING'
        self.assertIsNone(self.project(row)['receipt'])


class RunnerWiringTests(unittest.TestCase):
    def test_node_runner_with_real_cli_bridge_and_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(['node', '--test', 'scripts/test_mexico_e1_runner.cjs'], cwd=ROOT,
                env={**os.environ, 'THUNDER_TEST_TOQUES_PATH': str(DEPENDENCY),
                     'THUNDER_TEST_SQLITE_DIR': directory, 'THUNDER_TEST_PYTHON': sys.executable},
                capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            print(result.stdout)
            paths = list(Path(directory).glob('*.db'))
            self.assertGreater(len(paths), 15)
            statuses = []
            for path in paths:
                with closing(sqlite3.connect(path)) as conn:
                    statuses.extend(row[0] for row in conn.execute('SELECT status FROM toques'))
            self.assertIn('feito', statuses)
            self.assertIn('incerto', statuses)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--fixture':
        create_fixture(sys.argv[2])
    else:
        unittest.main()
