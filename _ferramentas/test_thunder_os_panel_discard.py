"""Offline HTTP/Banco fixtures; no providers, CRM, production DB or credentials."""
import concurrent.futures
import http.client
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import painel_servidor as panel


class PanelDiscardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bank = panel.Banco(root / 'brain', root / 'panel.db')
        self.bank.salvar('itens', {'status': 'pendente', 'tipo': 'dm', 'texto': 'fixture'}, id='pending', criar=True)
        self.env = patch.dict(panel.os.environ, {'THUNDER_EXTERNAL_EFFECTS_ENABLED': 'true',
            'THUNDER_OS_COPILOT_MODE': 'human_approval', 'THUNDER_OS_ACTORS': 'ygor,codex,hermes'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.server = panel.ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        self.server.banco = self.bank
        self.server.thunder_service = SimpleNamespace()
        self.server.thunder_nonces = {}
        self.server.thunder_origin = 'http://127.0.0.1:' + str(self.server.server_port)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        # Any accidental forwarding in this workflow fails the test.
        self.forward = patch.object(self.bank, 'aprovar', side_effect=AssertionError('provider/queue forbidden'))
        self.forward.start(); self.addCleanup(self.forward.stop)

    def request(self, method, path, body=None, actor='ygor', nonce=True, origin=None):
        headers = {'X-Authenticated-User': actor} if actor else {}
        if method == 'POST' and nonce:
            status, session = self.request('GET', '/api/os/v1/session', actor=actor)
            if status == 200: headers['X-Thunder-Nonce'] = session['nonce']
        if body is not None: headers['Content-Type'] = 'application/json'
        if method != 'GET': headers['Origin'] = origin or self.server.thunder_origin
        con = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            con.request(method, path, None if body is None else json.dumps(body), headers)
            result = con.getresponse()
            return result.status, json.loads(result.read())
        finally: con.close()

    def discard(self, **kwargs):
        return self.request('POST', '/api/os/v1/items/pending/discard', {}, **kwargs)

    def test_pending_discard_readback_and_count(self):
        before = self.request('GET', '/api/os/v1/items')[1]
        self.assertEqual(sum(i['status'] == 'pendente' for i in before), 1)
        status, result = self.discard()
        self.assertEqual(status, 200)
        self.assertEqual(result['status'], 'descartado')
        after = self.request('GET', '/api/os/v1/items')[1]
        self.assertEqual(sum(i['status'] == 'pendente' for i in after), 0)
        self.assertEqual(after[0]['_descarte']['ator'], 'ygor')
        self.assertFalse((self.bank.raiz / 'fila-envios.md').exists())
        self.assertFalse((self.bank.raiz / '_log/atividade.jsonl').exists())

    def test_unsafe_states_refused(self):
        samples = [{'status': s} for s in ('na-fila','enviado','aprovado','erro','copiar','incerto','needs_reconciliation','aguardando-confirmacao')]
        samples += [{'status': 'pendente', k: True} for k in ('fila_ref','_fila_pendente','uncertain','needs_reconciliation','receipt_ref','effect_id','job_id','dispatch_started_at')]
        for sample in samples:
            with self.subTest(sample=sample):
                with self.bank.conexao() as con: self.bank.gravar(con, 'itens', 'pending', sample)
                status, result = self.discard()
                self.assertEqual(status, 409)
                self.assertTrue(result['erro'])
                self.assertEqual(self.bank.listar('itens')[0]['status'], sample['status'])

    def test_repeat_is_idempotent(self):
        first = self.discard()
        second = self.discard()
        self.assertEqual(first, second)
        self.assertEqual(self.bank.listar('itens')[0]['_descarte'], first[1]['_descarte'])

    def test_authentication_required(self):
        self.assertEqual(self.discard(actor=None, nonce=False)[0], 401)
        self.assertEqual(self.discard(nonce=False)[0], 401)
        self.assertEqual(self.discard(actor='codex')[0], 403)
        self.assertEqual(self.discard(origin='http://evil.example')[0], 401)
        self.assertEqual(self.request('GET','/api/os/v1/items',actor=None)[0],401)
        self.assertEqual(self.request('POST','/api/os/v1/items/pending/discard',{'status':'aprovado'})[0],400)
        self.assertEqual(self.bank.listar('itens')[0]['status'],'pendente')

    def test_discard_serializes_with_forwarding(self):
        # Both supported writers share the file lock. Hold it to release a real race.
        self.forward.stop()
        def forward(con, id, data, previous):
            data.update(status='na-fila', fila_ref='fixture:queue')
            return data
        with patch.object(self.bank, 'aprovar', side_effect=forward):
            def approve():
                try: return self.bank.salvar('itens', {'status':'aprovado'}, id='pending')['status']
                except panel.Erro as e: return e.status
            def discard():
                try: return self.bank.descartar_pendente('pending','ygor')['status']
                except panel.Erro as e: return e.status
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                with self.bank.lock('_log/.painel.lock'):
                    a=pool.submit(approve); b=pool.submit(discard)
                outcomes=[a.result(),b.result()]
            self.assertIn(409,outcomes)
            row=self.bank.listar('itens')[0]
            self.assertIn(row['status'],('na-fila','descartado'))
            self.assertFalse(row.get('_descarte') and row.get('fila_ref'))
        # Deterministically exercise losing approval after a committed discard too.
        with self.bank.conexao() as con: self.bank.gravar(con,'itens','pending',{'status':'pendente'})
        self.bank.descartar_pendente('pending','ygor')
        with self.assertRaisesRegex(panel.Erro,'terminal'):
            self.bank.salvar('itens', {'status':'aprovado'},id='pending')

    def test_legacy_patch_stays_blocked(self):
        status,_=self.request('PATCH','/api/itens/pending',{'status':'descartado'})
        self.assertEqual(status,503)
        status,caps=self.request('GET','/api/os/v1/panel-capabilities')
        self.assertEqual(status,200)
        self.assertFalse(caps['legacy_mutations'])
        self.assertTrue(caps['reason'])


if __name__ == '__main__': unittest.main(verbosity=2)
