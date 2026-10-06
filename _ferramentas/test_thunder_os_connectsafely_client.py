import copy
import json
import unittest
from thunder_os.connectsafely_client import ConnectSafelyClient, ConnectSafelyError

ACCOUNT='a'*24
OWNER='urn:li:fsd_profile:OWNER'
RECIPIENT='urn:li:fsd_profile:RECIPIENT'
THREAD='urn:li:msg_conversation:THREAD'
NOW=1800000000
PLAN=dict(account_id=ACCOUNT,account_public_id='ygorbuenobr',action='dm',recipient_public_id='fixture-person',recipient_profile_urn=RECIPIENT,channel='linkedin',messaging_channel='linkedin_inbox',content='Synthetic message',conversation_urn=THREAD)
ROW=dict(id=ACCOUNT,publicId='ygorbuenobr',firstName='Ygor',status='AVAILABLE',enabled=True,apiSeatAllocated=True,cookies={'secret':'SECRET'})
MSG=dict(messageUrn='urn:li:messagingMessage:ONE',text=PLAN['content'],sentAt=NOW*1000,senderProfileId=OWNER,isSentByOwner=True,cookies='SECRET')

class ClientTests(unittest.TestCase):
    def setUp(self):
        self.calls=[];self.account=copy.deepcopy(ROW);self.message=copy.deepcopy(MSG)
        self.quota_rows=[dict(feature=f,window='daily',used=0,limit=1,remaining=1,limitReached=False,resetAt='2040-01-01T00:00:00Z') for f in ['CONNECT','synthetic_dm']]
        self.post=None;self.more=False;self.participants=[{'profileId':OWNER},{'profileId':RECIPIENT}]
        self.client=ConnectSafelyClient(ACCOUNT,'ygorbuenobr',request=self.request,clock=lambda:NOW,self_profile_id=OWNER,dm_quota_feature='synthetic_dm')
    def request(self,method,path,query,body):
        self.calls.append((method,path,query,body))
        if path.endswith('/accounts'):return 200,[self.account]
        if path.endswith('/quota'):return 200,dict(accountId=ACCOUNT,quotas=self.quota_rows)
        if '/exists/' in path:return 200,dict(success=True,accountId=ACCOUNT,profileUrn=RECIPIENT,hasConversation=True,conversationUrn=THREAD)
        if method=='POST':
            if isinstance(self.post,Exception):raise self.post
            return self.post or (200,dict(success=True,accountId=ACCOUNT,recipientProfileUrn=RECIPIENT,sentMessage=self.message))
        if path.endswith('/messages'):return 200,dict(success=True,accountId=ACCOUNT,messages=[self.message],participants=self.participants,hasMore=self.more)
        return 200,dict(success=True,accounts=[{'id':ACCOUNT}],conversations=[dict(conversationUrn=THREAD,source='linkedin',participants=self.participants,cookies='SECRET')],hasMore=False)
    def test_default_fail_closed(self):
        with self.assertRaisesRegex(ConnectSafelyError,'unconfigured'):ConnectSafelyClient(ACCOUNT,'ygorbuenobr').accounts()
    def test_account_filter_and_secrets_not_returned(self):
        self.assertNotIn('SECRET',json.dumps(self.client.accounts()))
        self.assertTrue(self.client.verify_identity()['verified'])
        self.assertEqual(self.calls[0][2],{'q':ACCOUNT})
        for key,value in [('firstName',{'cookies':'SECRET'}),('enabled',1),('publicId',['SECRET'])]:
            self.account=copy.deepcopy(ROW);self.account[key]=value
            with self.assertRaisesRegex(ConnectSafelyError,'schema_invalid'):self.client.accounts()
    def test_identity_gate_no_write(self):
        for key,value in [('id','b'*24),('publicId','other'),('status','WARMUP'),('enabled',False)]:
            self.account=copy.deepcopy(ROW);self.account[key]=value
            with self.assertRaises(ConnectSafelyError):self.client.send(PLAN,before_dispatch=lambda:True)
        self.assertFalse(any(c[0]=='POST' for c in self.calls))
    def test_seat_metadata_does_not_override_provider_authorization(self):
        self.account['apiSeatAllocated']=False
        self.assertEqual(self.client.send(PLAN,before_dispatch=lambda:True)['status'],'sent')
        self.assertEqual(sum(c[0]=='POST' for c in self.calls),1)
        self.assertIs(self.account['apiSeatAllocated'],False)
    def test_seat_metadata_never_bypasses_quota_or_dispatch_denial(self):
        self.account['apiSeatAllocated']=False
        original=self.request
        for status in (401,403,429):
            self.calls=[]
            def request(method,path,query,body):
                if path.endswith('/quota'):
                    self.calls.append((method,path,query,body));return status,{'error':'PRIVATE'}
                return original(method,path,query,body)
            self.client.request=request
            with self.assertRaises(ConnectSafelyError):self.client.send(PLAN,before_dispatch=lambda:True)
            self.assertFalse(any(c[0]=='POST' for c in self.calls))
        self.client.request=original;self.calls=[]
        with self.assertRaisesRegex(ConnectSafelyError,'dispatch_authority_denied'):
            self.client.send(PLAN,before_dispatch=lambda:False)
        self.assertFalse(any(c[0]=='POST' for c in self.calls))
    def test_seat_metadata_false_write_denial_is_single_unconfirmed_attempt(self):
        self.account['apiSeatAllocated']=False
        for status,code in ((401,'provider_unauthorized'),(403,'provider_forbidden')):
            self.calls=[];self.post=(status,{'error':'PRIVATE'})
            result=self.client.send(PLAN,before_dispatch=lambda:True)
            self.assertEqual(result,{'status':'uncertain','error_code':code})
            self.assertEqual(sum(c[0]=='POST' for c in self.calls),1)

    def test_duplicate_identity(self):
        self.client.request=lambda *args:(200,[ROW,ROW])
        with self.assertRaisesRegex(ConnectSafelyError,'identity'):self.client.verify_identity()
    def test_bounded_scoped_reads_and_nested_secrets(self):
        self.assertNotIn('SECRET',json.dumps(self.client.conversations()))
        self.assertNotIn('SECRET',json.dumps(self.client.messages(THREAD)))
        self.assertTrue(all(c[2].get('accountId')==ACCOUNT for c in self.calls if not c[1].endswith('/accounts')))
        self.assertTrue(all(c[0]=='GET' for c in self.calls))
        for value in [0,11,True]:
            with self.assertRaisesRegex(ConnectSafelyError,'limit'):self.client.conversations(value)
        self.participants=[{'profileId':{'cookies':'SECRET'}}]
        with self.assertRaisesRegex(ConnectSafelyError,'schema_invalid'):self.client.messages(THREAD)
    def test_exact_modern_send_and_receipt(self):
        result=self.client.send(PLAN,before_dispatch=lambda:True);self.assertEqual(result['status'],'sent')
        writes=[c for c in self.calls if c[0]=='POST'];self.assertEqual(len(writes),1)
        self.assertEqual(writes[0][1],'/linkedin/conversations/send')
        self.assertEqual(writes[0][3]['messagingChannel'],'linkedin_inbox')
        self.assertEqual(writes[0][3]['accountId'],ACCOUNT)
        self.assertNotIn('SECRET',json.dumps(result))
    def test_unknown_write_denial_no_retry_no_raw_error(self):
        for outcome in [RuntimeError('SECRET'),(401,{'error':'SECRET'}),(429,{'cookies':'SECRET'}),(500,{'error':'SECRET'})]:
            self.post=outcome;self.calls=[];result=self.client.send(PLAN,before_dispatch=lambda:True)
            self.assertEqual(result['status'],'uncertain');self.assertNotIn('SECRET',json.dumps(result))
            self.assertEqual(sum(c[0]=='POST' for c in self.calls),1)
    def test_invalid_receipts_and_missing_owner_pin(self):
        for key,value in [('messageUrn','urn:li:fsd_profile:WRONGTYPE'),('messageUrn','fake'),('text','different'),('sentAt',NOW-1),('senderProfileId','someone'),('isSentByOwner',False)]:
            self.message=copy.deepcopy(MSG);self.message[key]=value
            self.assertEqual(self.client.send(PLAN,before_dispatch=lambda:True)['status'],'uncertain')
        self.message=copy.deepcopy(MSG);self.client.self_profile_id=None
        self.assertEqual(self.client.send(PLAN,before_dispatch=lambda:True)['status'],'uncertain')
        self.post=(200,dict(success=True,accountId='b'*24,sentMessage=MSG))
        self.assertEqual(self.client.send(PLAN,before_dispatch=lambda:True)['status'],'uncertain')
    def test_invite_success_cannot_invent_receipt(self):
        plan={**PLAN,'action':'invite','content':'','conversation_urn':None}
        self.post=(200,dict(success=True,accountId=ACCOUNT,profileUrn=RECIPIENT))
        result=self.client.send(plan,before_dispatch=lambda:True);self.assertEqual(result['status'],'uncertain');self.assertNotIn('receipt_ref',result)
        self.calls=[];self.assertEqual(self.client.reconcile(plan,NOW)['status'],'uncertain');self.assertTrue(all(c[0]=='GET' for c in self.calls))
    def test_reconcile_exact_participants_content_time_no_post(self):
        result=self.client.reconcile(PLAN,NOW);self.assertEqual(result['status'],'sent');self.assertFalse(result['write_attribution_confirmed']);self.assertEqual(result['confirmation_kind'],'observed_outbound')
        self.assertTrue(all(c[0]=='GET' for c in self.calls))
        self.more=True;self.assertEqual(self.client.reconcile(PLAN,NOW)['status'],'uncertain')
        self.more=False;self.participants=[{'profileId':OWNER},{'profileId':'wrong'}]
        self.assertEqual(self.client.reconcile(PLAN,NOW)['status'],'uncertain')
    def test_reconcile_late_copy_outside_attempt_window_not_attributed(self):
        self.client.clock=lambda:NOW+3600
        self.message['sentAt']=(NOW+3600)*1000
        self.assertEqual(self.client.reconcile(PLAN,NOW)['status'],'uncertain')
    def test_quota_unknown_malformed_and_exhausted_never_post(self):
        for updates in [{'remaining':0,'used':1},{'limitReached':True},{'resetAt':'invalid'},{'used':False},{'window':'unknown'},{'remaining':8}]:
            original=copy.deepcopy(self.quota_rows);self.quota_rows[1].update(updates);self.calls=[]
            with self.assertRaises(ConnectSafelyError):self.client.send(PLAN,before_dispatch=lambda:True)
            self.assertFalse(any(c[0]=='POST' for c in self.calls));self.quota_rows=original
        self.client.dm_quota_feature=None
        with self.assertRaisesRegex(ConnectSafelyError,'quota_contract_unverified'):self.client.send(PLAN,before_dispatch=lambda:True)
    def test_dispatch_recheck_after_all_reads_immediately_before_write(self):
        events=[]
        original=self.client.request
        def request(method,path,query,body):
            events.append(method+':'+path)
            return original(method,path,query,body)
        self.client.request=request
        def authority():events.append('APPROVAL');return True
        self.client.send(PLAN,before_dispatch=authority)
        self.assertEqual(events[-2:],['APPROVAL','POST:/linkedin/conversations/send'])
        for callback in [lambda:False,lambda:None]:
            self.calls=[]
            with self.assertRaisesRegex(ConnectSafelyError,'dispatch_authority_denied'):
                self.client.send(PLAN,before_dispatch=callback)
            self.assertFalse(any(c[0]=='POST' for c in self.calls))
        def revoked():raise ConnectSafelyError('approval_revoked')
        self.calls=[]
        with self.assertRaisesRegex(ConnectSafelyError,'approval_revoked'):
            self.client.send(PLAN,before_dispatch=revoked)
        self.assertFalse(any(c[0]=='POST' for c in self.calls))
        with self.assertRaises(TypeError):self.client.send(PLAN)
    def test_unknown_pagination_or_conversation_mismatch_cannot_confirm(self):
        original=self.client.request
        for field,value in [('hasMore',None),('hasMore','true'),('hasMore',1),('conversationUrn','urn:li:msg_conversation:OTHER'),('accountId','b'*24)]:
            def changed(method,path,query,body):
                code,data=original(method,path,query,body)
                if path.endswith('/messages'):
                    data[field]=value
                return code,data
            self.client.request=changed
            with self.assertRaises(ConnectSafelyError):self.client.reconcile(PLAN,NOW)
        def missing(method,path,query,body):
            code,data=original(method,path,query,body)
            if path.endswith('/messages') or path.endswith('/conversations'):data.pop('hasMore',None)
            return code,data
        self.client.request=missing
        with self.assertRaisesRegex(ConnectSafelyError,'pagination_unverified'):self.client.reconcile(PLAN,NOW)
        with self.assertRaisesRegex(ConnectSafelyError,'pagination_unverified'):self.client.conversations()
        self.assertFalse(any(c[0]=='POST' for c in self.calls))
    def test_group_duplicate_or_missing_participants_cannot_confirm_dm(self):
        for participants in [[OWNER,RECIPIENT,'urn:li:fsd_profile:THIRD'],[OWNER,RECIPIENT,RECIPIENT],[OWNER],[RECIPIENT,RECIPIENT]]:
            self.participants=[{'profileId':pid} for pid in participants]
            self.assertEqual(self.client.reconcile(PLAN,NOW)['status'],'uncertain')
    def test_extra_fields_channel_and_malformed_plan(self):
        for updates in [{'cookies':'SECRET'},{'messaging_channel':'auto'},{'recipient_profile_urn':None},{'content':'\ud800'},{'conversation_urn':'urn:li:salesNav_thread:x'}]:
            with self.assertRaises(ConnectSafelyError):self.client.send({**PLAN,**updates},before_dispatch=lambda:True)
        self.assertEqual(self.calls,[])

class InvitationReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        self.plan={**PLAN,'action':'invite','content':'','conversation_urn':None}
        self.relation=dict(accountId=ACCOUNT,profileUrn=RECIPIENT,status='INVITATION_SENT',
                           connected=False,invitationSent=True,invitationReceived=False)
        self.row=dict(id='123',invitationId='123',invitationUrn='urn:li:invitation:123',
                      memberId='RECIPIENT',memberUrn=RECIPIENT,invitationType='SENT',sentAt=NOW,
                      profile=dict(publicIdentifier='fixture-person',profileUrl='https://www.linkedin.com/in/fixture-person'))
        self.rows=[self.row];self.complete=True;self.http=200;self.page_account=ACCOUNT
        self.client=ConnectSafelyClient(ACCOUNT,'ygorbuenobr',request=self.request,clock=lambda:NOW+30,self_profile_id=OWNER)
    def request(self,method,path,query,body):
        self.calls.append((method,path,query,body));self.assertEqual('GET',method)
        if path.endswith('/accounts'):return 200,[ROW]
        if '/relationship/' in path:return 200,self.relation
        self.assertEqual('/linkedin/invitations/sent',path)
        rows=copy.deepcopy(self.rows)
        if not self.complete:
            for i,row in enumerate(rows):
                identity=str(1000+query['startIndex']+i)
                row.update(id=identity,invitationId=identity,invitationUrn='urn:li:invitation:'+identity)
        return self.http,dict(success=True,accountId=self.page_account,invitations=rows,count=len(rows),
                             startIndex=query['startIndex'],nextStartIndex=query['startIndex']+len(rows),endOfList=self.complete)
    def test_precise_unique_receipt_is_observed_sent_not_accepted_or_attributed(self):
        for stamp in (NOW,NOW*1000,'2027-01-15T08:00:00+00:00'):
            self.row['sentAt']=stamp
            result=self.client.reconcile(self.plan,NOW)
            self.assertEqual('sent',result['status']);self.assertEqual('observed_outbound',result['confirmation_kind'])
            self.assertFalse(result['write_attribution_confirmed'])
            self.assertEqual('connectsafely:urn:li:invitation:123',result['receipt_ref'])
            self.assertEqual('INVITATION_SENT',result['invitation_observation']['relationship_status'])
            self.assertNotIn('accepted',result)
        self.assertTrue(all(x[0]=='GET' for x in self.calls))
    def test_old_future_relative_missing_timezone_and_nonfinite_time_never_confirm(self):
        for stamp in (NOW-1,NOW+121,'Sent today','Sent 3 minutes ago','2027-01-15T08:00:00',None,True,float('nan')):
            with self.subTest(stamp=stamp):
                self.row['sentAt']=stamp
                self.assertEqual('uncertain',self.client.reconcile(self.plan,NOW)['status'])
    def test_wrong_account_target_id_urn_and_note_never_confirm(self):
        baseline=copy.deepcopy(self.row)
        for field,value in [('memberUrn','urn:li:fsd_profile:OTHER'),('memberId','OTHER'),
                            ('id','other'),('invitationId','other'),('invitationUrn','urn:li:invitation:other'),
                            ('customMessage','Different content'),('invitationType','RECEIVED')]:
            self.row.clear();self.row.update(copy.deepcopy(baseline));self.row[field]=value
            self.assertEqual('uncertain',self.client.reconcile(self.plan,NOW)['status'])
        self.row.clear();self.row.update(baseline)
        self.page_account='b'*24
        self.assertEqual('uncertain',self.client.reconcile(self.plan,NOW)['status'])
        self.page_account=ACCOUNT
        for field,value in [('accountId','b'*24),('profileUrn','urn:li:fsd_profile:OTHER'),('invitationSent',1)]:
            old=self.relation[field];self.relation[field]=value
            self.assertEqual('uncertain',self.client.reconcile(self.plan,NOW)['status']);self.relation[field]=old
        self.row['profile']['publicIdentifier']='different-person'
        self.row['profile']['profileUrl']='https://www.linkedin.com/in/different-person'
        self.assertEqual('uncertain',self.client.reconcile(self.plan,NOW)['status'])
    def test_note_requires_exact_observed_content(self):
        plan={**self.plan,'content':'Approved fixture note'}
        self.assertEqual('uncertain',self.client.reconcile(plan,NOW)['status'])
        self.row['customMessage']=plan['content']
        self.assertEqual('sent',self.client.reconcile(plan,NOW)['status'])
    def test_partial_pages_duplicate_receipts_and_denial_stay_uncertain(self):
        self.complete=False
        result=self.client.reconcile(self.plan,NOW)
        self.assertEqual('history_incomplete',result['error_code'])
        self.assertEqual(3,sum(x[1].endswith('/invitations/sent') for x in self.calls))
        self.complete=True;self.rows=[self.row,copy.deepcopy(self.row)]
        self.assertEqual('uncertain',self.client.reconcile(self.plan,NOW)['status'])
        self.rows=[self.row,{**self.row,'id':'456','invitationId':'456','invitationUrn':'urn:li:invitation:456'}]
        self.assertEqual('uncertain',self.client.reconcile(self.plan,NOW)['status'])
        self.rows=[self.row]
        for status in (401,403,429,500):
            self.http=status;self.calls=[]
            result=self.client.reconcile(self.plan,NOW)
            self.assertEqual('uncertain',result['status'])
            self.assertEqual('INVITATION_SENT',result['invitation_observation']['relationship_status'])
            self.assertEqual(1,sum(x[1].endswith('/invitations/sent') for x in self.calls))
    def test_accepted_later_is_only_relationship_observation(self):
        for status,flags in [('CONNECTED',(True,False,False)),('INVITATION_RECEIVED',(False,False,True)),('NOT_CONNECTED',(False,False,False))]:
            self.calls=[];self.relation.update(status=status,**dict(zip(('connected','invitationSent','invitationReceived'),flags)))
            result=self.client.reconcile(self.plan,NOW)
            self.assertEqual('uncertain',result['status']);self.assertNotIn('receipt_ref',result)
            self.assertEqual(status,result['invitation_observation']['relationship_status'])
            self.assertFalse(any(x[1].endswith('/invitations/sent') for x in self.calls))
    def test_directory_accepts_valid_unicode_profiles_without_weakening_binding(self):
        other=copy.deepcopy(self.row);other.update(id='456',invitationId='456',invitationUrn='urn:li:invitation:456')
        other['profile'].update(publicIdentifier='joão.silva',profileUrl='https://www.linkedin.com/in/jo%C3%A3o.silva')
        self.rows=[other,self.row]
        self.assertEqual('sent',self.client.reconcile(self.plan,NOW)['status'])
        other['profile']['profileUrl']='https://www.linkedin.com/in/wrong'
        self.assertEqual('uncertain',self.client.reconcile(self.plan,NOW)['status'])

if __name__=='__main__':unittest.main()
