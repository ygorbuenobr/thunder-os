"""Authenticated Mission A routes with an inert adapter fixture."""
import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock,patch
import threading,time,unittest
import painel_servidor as panel

class MissionAHTTPTests(unittest.TestCase):
    def setUp(self):
        trust=patch.object(panel.Handler,'trusted_confirmation_peer',return_value=True); trust.start(); self.addCleanup(trust.stop)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),panel.Handler)
        self.server.banco=SimpleNamespace(); self.server.thunder_service=Mock()
        self.server.thunder_origin='https://panel.example.invalid'; self.server.thunder_nonces={}
        self.server.thunder_mission_a=Mock()
        self.server.thunder_mission_a.request.return_value={'status':'scheduled','job_id':'abc123def456'}
        self.server.thunder_mission_a.result.return_value={'status':'pending'}
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.addCleanup(self.stop)
    def stop(self): self.server.shutdown(); self.server.server_close(); self.thread.join(2)
    def call(self,path,method='GET',body=None,actor='ygor',nonce=False):
        c=HTTPConnection('127.0.0.1',self.server.server_port,timeout=3); self.addCleanup(c.close)
        h={'X-Authenticated-User':actor}
        if body is not None: h['Content-Type']='application/json'
        if nonce:
            token=str(time.monotonic_ns()); self.server.thunder_nonces[token]=(actor,time.monotonic()+30)
            h.update({'X-Thunder-Nonce':token,'Origin':self.server.thunder_origin})
        c.request(method,path,None if body is None else json.dumps(body),h)
        r=c.getresponse(); return r.status,json.loads(r.read())
    def test_requires_explicit_cost_and_actor_and_reads_existing_request(self):
        path='/api/os/v1/mission-a'; body={'request_id':'fixture-1','cost_confirmed':True}
        self.assertEqual(self.call(path,'POST',body)[0],401)
        self.assertEqual(self.call(path,'POST',{**body,'cost_confirmed':False},nonce=True)[0],400)
        self.assertEqual(self.call(path,'POST',body,actor='codex',nonce=True)[0],403)
        self.server.thunder_mission_a.request.assert_not_called()
        self.assertEqual(self.call(path,'POST',body,nonce=True)[0],202)
        self.server.thunder_mission_a.request.assert_called_once_with('fixture-1',True)
        self.assertEqual(self.call(path+'/fixture-1')[1]['status'],'pending')
        self.assertEqual(self.call(path+'/fixture-1',actor='codex')[0],403)

    def test_unverified_delegate_model_returns_clear_503_without_job(self):
        from thunder_os.mission_a import MissionA, NativeScheduler
        repo = Mock(); source = Mock()
        self.server.thunder_mission_a = MissionA(repo, NativeScheduler(), source)
        with patch('thunder_os.mission_a.subprocess.run', return_value=SimpleNamespace(stdout='{}')) as run:
            status, body = self.call('/api/os/v1/mission-a', 'POST',
                {'request_id':'fixture-block','cost_confirmed':True}, nonce=True)
        self.assertEqual(status, 503)
        self.assertIn('GPT-6.1 Terra', body['erro'])
        self.assertEqual(run.call_count,1); source.assert_not_called(); repo.append_state.assert_not_called()

if __name__=='__main__': unittest.main()
