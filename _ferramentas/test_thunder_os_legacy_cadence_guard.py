"""Direct legacy API approval checks using local fixtures only."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import painel_servidor as panel


class LegacyCadenceGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bank = panel.Banco(root / 'brain', root / 'panel.db')
        self.env = patch.dict(panel.os.environ, {'THUNDER_EXTERNAL_EFFECTS_ENABLED':'true',
            'THUNDER_OS_COPILOT_MODE':'disabled', 'THUNDER_OS_EFFECT_MODE':'standard'})
        self.env.start();self.addCleanup(self.env.stop)
        self.server = panel.ThreadingHTTPServer(('127.0.0.1',0), panel.Handler)
        self.server.banco = self.bank
        thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        thread.start();self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.forward = patch.object(self.bank,'aprovar',side_effect=lambda con,ident,data,prior:data)
        self.mock_forward = self.forward.start();self.addCleanup(self.forward.stop)

    def request(self, method, path, body):
        con = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            con.request(method,path,json.dumps(body),{'Content-Type':'application/json'})
            response=con.getresponse()
            return response.status,json.loads(response.read())
        finally:
            con.close()

    def item(self, ident, cadence=None):
        row={'tipo':'decisao','status':'pendente'}
        if cadence is not None:row['cadencia']=cadence
        self.bank.salvar('itens',row,id=ident,criar=True)

    def test_external_unknown_and_forged_internal_cannot_approve(self):
        for i,cadence in enumerate(({'execution_context':{'platform':'hubspot_seq'}},
                                    {}, {'execution_context':{'platform':'thunder_os'}})):
            ident='case'+str(i);self.item(ident,cadence)
            status,_=self.request('PATCH','/api/itens/'+ident,{'status':'aprovado'})
            self.assertEqual(status,409)
            with self.bank.conexao() as con:self.assertEqual(self.bank.ler(con,'itens',ident)['status'],'pendente')
        self.mock_forward.assert_not_called()

    def test_cannot_remove_or_replace_binding_even_in_separate_request(self):
        for i,change in enumerate((None,{'execution_context':{'platform':'thunder_os'}})):
            ident='blocked'+str(i)
            self.item(ident,{'execution_context':{'platform':'hubspot_seq'}})
            self.assertEqual(self.request('PATCH','/api/itens/'+ident,
                {'cadencia':change,'status':'aprovado'})[0],409)
            self.assertEqual(self.request('PATCH','/api/itens/'+ident,{'cadencia':change})[0],409)
            self.assertEqual(self.request('PATCH','/api/itens/'+ident,{'status':'aprovado'})[0],409)
        self.mock_forward.assert_not_called()

    def test_no_cadence_approval_is_preserved(self):
        self.item('ordinary')
        status,result=self.request('PATCH','/api/itens/ordinary',{'status':'aprovado'})
        self.assertEqual((status,result['status']),(200,'aprovado'))
        self.mock_forward.assert_called_once()

    def test_approval_cannot_attach_binding_during_creation_or_update(self):
        self.item('ordinary')
        cadence={'execution_context':{'platform':'thunder_os'}}
        self.assertEqual(self.request('PATCH','/api/itens/ordinary',
            {'status':'aprovado','cadencia':cadence})[0],409)
        self.assertEqual(self.request('POST','/api/itens',
            {'id':'new','tipo':'decisao','status':'aprovado','cadencia':cadence})[0],409)
        self.mock_forward.assert_not_called()

    def test_cannot_attach_cadence_after_approval_or_to_existing_pending(self):
        cadence={'execution_context':{'platform':'thunder_os'}}
        self.item('ordinary')
        status,result=self.request('PATCH','/api/itens/ordinary',{'status':'aprovado'})
        self.assertEqual((status,result['status']),(200,'aprovado'))
        self.assertEqual(self.request('PATCH','/api/itens/ordinary',{'cadencia':cadence})[0],409)
        with self.bank.conexao() as con:
            saved=self.bank.ler(con,'itens','ordinary')
        self.assertEqual(saved['status'],'aprovado')
        self.assertNotIn('cadencia',saved)
        self.item('pending')
        self.assertEqual(self.request('PATCH','/api/itens/pending',{'cadencia':cadence})[0],409)


if __name__ == '__main__':
    unittest.main()
