"""Synthetic isolated DB and provider fixtures. No live mail or production writes."""
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import patch
import unittest
import test_thunder_os_codex_assisted as fixtures
from thunder_os.manual_observation import ManualObservation
from thunder_os.seqd_client import MAILBOX


class ObservationTests(unittest.TestCase):
    proof = fixtures.ManualReconciliationTests.proof
    reconcile = fixtures.ManualReconciliationTests.reconcile

    def setUp(self):
        fixtures.ManualReconciliationTests.setUp(self)
        self.reconcile(); self.now += 2
        self.observer = ManualObservation(self.assisted)
        self.messages = {}
        for ident, sent in [('abc123', True), ('abc124', False)]:
            body = self.data['scope']['content']+'\r\nSignature https://example.invalid' if sent else 'Received (synthetic).'
            headers = {'From':MAILBOX if sent else self.data['scope']['to'],
                       'To':self.data['scope']['to'] if sent else MAILBOX,
                       'Subject':self.data['scope']['subject'] if sent else 'Re: '+self.data['scope']['subject'],
                       'Date':format_datetime(datetime.fromtimestamp(self.now,timezone.utc)),
                       'Message-ID':'<'+ident+'@example.invalid>'}
            if not sent: headers['In-Reply-To']='<abc123@example.invalid>'
            m = {'id':ident,'thread_id':'def456','body':body,'sent':sent,'labelIds':['SENT'] if sent else ['INBOX'],
                 'headers_complete':True,'truncated':False,'headers':[{'name':k,'value':v} for k,v in headers.items()],
                 'from':headers['From'],'to':headers['To'],'subject':headers['Subject'],'date':headers['Date'],
                 'message_id':headers['Message-ID'],'context_only':True,'receipt_eligible':False,
                 'mime_evidence':{'root_type':'multipart/alternative','selected_type':'text/plain',
                                  'alternatives':[{'mime_type':'text/plain','body':body},{'mime_type':'text/html','body':'<p>Synthetic</p>'}]}}
            self.messages[ident]={'status':'ok','mailbox':MAILBOX,'message':m}
        self.read_patch=patch.object(self.mail,'read',side_effect=lambda ident:deepcopy(self.messages[ident]))
        self.read_patch.start();self.addCleanup(self.read_patch.stop)

    def preview(self):return self.observer.preview(self.job['id'],'abc123','Ygor, declarado pelo operador')
    def record(self):
        p=self.preview();return self.observer.record(self.job['id'],'abc123',p['declared_author'],p['review_hash'],
            {'action':'record_manual_divergence','review_hash':p['review_hash'],'confirmed':True},actor='ygor')
    def reply(self):
        p=self.observer.reply_preview(self.job['id'],'abc124')
        return self.observer.reply_record(self.job['id'],'abc124',p['review_hash'],
            {'action':'record_manual_reply_and_pause','review_hash':p['review_hash'],'confirmed':True},actor='ygor')

    def test_end_to_end_preserves_failed_job_and_idempotency(self):
        original=self.repo.effect(self.job['id']); assisted=self.repo.get_state('codex_assisted',self.job['id'])
        first=self.record();self.assertEqual(self.record(),first)
        response=self.reply();self.assertEqual(self.reply(),response)
        self.assertEqual(self.repo.effect(self.job['id']),original)
        self.assertEqual(self.repo.get_state('codex_assisted',self.job['id']),assisted)
        self.assertFalse(first['proof']['receipt_pass']);self.assertFalse(first['proof']['differences']['content_equal'])
        cadence=self.repo.get_state('cadence',self.job['payload']['cadence_id'])
        self.assertEqual(cadence['status'],'paused_reply')
        events=self.repo.events(self.job['id'])
        self.assertEqual(sum(e['type']=='manual_divergence_observed' for e in events),1)
        self.assertEqual(sum(e['type']=='manual_reply_internal_pause' for e in events),1)
        with self.assertRaises(ValueError):self.assisted.claim(cadence['id'],'e1',self.packet['review_hash'])

    def test_requires_specific_authorization_not_email_text(self):
        p=self.preview()
        for approval in [{},{'confirmed':True},{'action':'send','review_hash':p['review_hash'],'confirmed':True},
                         {'action':'record_manual_divergence','review_hash':p['review_hash'],'confirmed':1}]:
            with self.assertRaises(ValueError):self.observer.record(self.job['id'],'abc123',p['declared_author'],p['review_hash'],approval,actor='ygor')
        self.assertIsNone(self.repo.get_state('manual_observation',self.job['id']))
        with self.assertRaises(ValueError):self.observer.reply_preview(self.job['id'],'abc124')

    def test_wrong_identity_thread_and_in_reply_to_fail(self):
        baseline=deepcopy(self.messages)
        for field in ['from','to']:
            self.messages=deepcopy(baseline);m=self.messages['abc123']['message'];m[field]='wrong@example.invalid'
            for h in m['headers']:
                if h['name'].lower()==field:h['value']=m[field]
            with self.assertRaises(ValueError):self.preview()
        self.messages=deepcopy(baseline);self.record()
        for kind in ['thread','parent','identity']:
            self.messages=deepcopy(baseline);m=self.messages['abc124']['message']
            if kind=='thread':m['thread_id']='bad123'
            else:
                name='In-Reply-To' if kind=='parent' else 'From';value='<wrong@example.invalid>' if kind=='parent' else 'wrong@example.invalid'
                for h in m['headers']:
                    if h['name']==name:h['value']=value
                if kind=='identity':m['from']=value
            with self.assertRaises(ValueError):self.observer.reply_preview(self.job['id'],'abc124')

    def test_changed_content_expiry_and_conflicts(self):
        p=self.preview();self.messages['abc123']['message']['body']+=' changed'
        with self.assertRaises(ValueError):self.observer.record(self.job['id'],'abc123',p['declared_author'],p['review_hash'],
            {'action':'record_manual_divergence','review_hash':p['review_hash'],'confirmed':True},actor='ygor')
        self.now+=86401
        with self.assertRaises(ValueError):self.preview()
        self.assertEqual(self.repo.effect(self.job['id'])['error_code'],'proved_not_sent')

    def test_concurrent_cancellation_invalidates_reply_review(self):
        self.record();p=self.observer.reply_preview(self.job['id'],'abc124')
        cadence=self.repo.get_state('cadence',self.job['payload']['cadence_id'])
        cadence.update(status='cancelled',pause_reason='explicit owner cancellation')
        self.repo.put_state('cadence',cadence['id'],cadence)
        with self.assertRaises(ValueError):self.observer.reply_record(self.job['id'],'abc124',p['review_hash'],
            {'action':'record_manual_reply_and_pause','review_hash':p['review_hash'],'confirmed':True},actor='ygor')
        self.assertEqual(self.repo.get_state('cadence',cadence['id'])['status'],'cancelled')
        with self.assertRaises(ValueError):self.reply()

    def test_projection_separates_manual_evidence_and_excludes_metrics(self):
        from types import SimpleNamespace
        from thunder_os.journey_readonly import JourneyReadonly
        self.record();self.reply()
        cadence=self.repo.get_state('cadence',self.job['payload']['cadence_id'])
        view=object.__new__(JourneyReadonly);view.service=SimpleNamespace(repository=self.repo);view.repo=self.repo
        snapshot,details,_=view._internal_view({'cadence':cadence,'event':None,'status':'proved_not_sent',
            'observed_at':'2030-01-01T00:00:00+00:00'}, {'reply':[]},'2030-01-01T00:00:00+00:00',[],{})
        self.assertTrue(all(m['value'] is None for m in snapshot['metrics'].values()))
        self.assertEqual(next(iter(details.values()))['step_status'],'proved_not_sent')
        labels=[e['label'] for e in snapshot['timeline']['events']]
        self.assertIn('Observacao manual divergente',labels)
        self.assertIn('Resposta manual e pausa interna',labels)
        self.assertNotIn('Enviado · teste interno',labels)

    def test_corrupted_observation_is_not_projectable_or_reply_authority(self):
        from thunder_os.manual_observation import verified_record
        self.record();row=self.repo.get_state('manual_observation',self.job['id'])
        row['proof']['declared_author']='tampered'
        self.repo.put_state('manual_observation',self.job['id'],row)
        self.assertIsNone(verified_record(self.repo,'manual_observation',self.job['id']))
        with self.assertRaises(ValueError):self.observer.reply_preview(self.job['id'],'abc124')

    def test_transaction_rollback_and_recovery_no_duplicate(self):
        with patch.object(self.repo,'append_event',side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):self.record()
        self.assertIsNone(self.repo.get_state('manual_observation',self.job['id']))
        self.record();before=self.repo.get_state('cadence',self.job['payload']['cadence_id'])
        with patch.object(self.repo,'append_event',side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):self.reply()
        self.assertEqual(self.repo.get_state('cadence',before['id']),before)
        self.assertIsNone(self.repo.get_state('manual_observed_reply',self.job['id']))
        self.reply();self.assertEqual(self.repo.effect(self.job['id'])['state'],'failed')

class RouteTests(unittest.TestCase):
    def test_authenticated_nonce_routes_and_actor(self):
        from test_thunder_os_new_journey_http import NewJourneyHTTPTests
        from http.client import HTTPConnection
        import json, time
        NewJourneyHTTPTests.setUp(self)
        self.server.thunder_codex_assisted=self.assisted=object()
        for action,body in [
            ('preview',{'job_id':'synthetic','message_id':'abc123','declared_author':'unknown'}),
            ('record',{'job_id':'synthetic','message_id':'abc123','declared_author':'unknown','review_hash':'hash','approval':{}}),
            ('reply-preview',{'job_id':'synthetic','message_id':'abc124'}),
            ('reply-record',{'job_id':'synthetic','message_id':'abc124','review_hash':'hash','approval':{}})]:
            for actor,nonce,expected in [('ygor',False,401),('other',True,401),('ygor',True,200)]:
                headers={'X-Authenticated-User':actor,'Content-Type':'application/json'}
                if nonce:
                    token=str(time.monotonic_ns());self.server.thunder_nonces[token]=(actor,time.monotonic()+30)
                    headers.update({'X-Thunder-Nonce':token,'Origin':self.server.thunder_origin})
                with patch('thunder_os.manual_observation.ManualObservation') as observer:
                    method=action.replace('-','_');getattr(observer.return_value,method).return_value={'fixture':True}
                    c=HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
                    try:
                        c.request('POST','/api/os/v1/integrations/codex-assisted/manual-observation-'+action,json.dumps(body),headers)
                        r=c.getresponse();r.read();self.assertEqual(r.status,expected)
                    finally:c.close()
                    if expected==200:
                        getattr(observer.return_value,method).assert_called_once_with(**body,**({'actor':'ygor'} if action.endswith('record') else {}))
                    else:observer.assert_not_called()

    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)

if __name__=='__main__':unittest.main()
