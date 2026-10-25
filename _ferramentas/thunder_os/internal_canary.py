"""Explicit internal one-email test; never qualifies a commercial account.

Uses the existing registry, cadence, approval and effect ledger. Only this scoped
service recognizes a canary: the general worker retains its commercial gates.
"""
from copy import deepcopy
import re
import time
from .service import ThunderService
from .models import payload_hash
from .thunder_email_runtime import require, pinned_policy
from .approvals import _step_hash
from .seqd_client import MAILBOX

PREFIX='internal-canary-'

class InternalCanaryService(ThunderService):
    def __init__(self,base,policy_loader=None):
        super().__init__(base.repository,base.ledger,base.hubspot,knowledge_store=base.knowledge_store)
        self.base=base
        self.policy_loader=policy_loader or (lambda:pinned_policy("THUNDER_OS_CODEX_ASSISTED_POLICY"))

    def check_policy(self,ident,scope,approval_ref):
        p=self.policy_loader()
        now=time.time()
        step={'id':'e1','recipient':scope['to'],'channel':'email','subject':scope['subject'],
              'content':scope['content'],'order':1,'interval_hours':0}
        require(p.get('version')==1 and p.get('mailbox')==MAILBOX and p.get('campaign_id')==ident
            and p.get('cadence_id')==ident and p.get('step_id')=='e1' and p.get('recipient')==scope['to']
            and p.get('subject')==scope['subject'] and p.get('content_hash')==payload_hash(scope['content'])
            and p.get('effect_key')==_step_hash(step) and p.get('approval_ref')==approval_ref
            and type(p.get('max_attempts')) is int and p['max_attempts']==1
            and type(p.get('issued_at')) in (int,float) and type(p.get('expires_at')) in (int,float)
            and p['issued_at']<=now<p['expires_at'] and 0<p['expires_at']-p['issued_at']<=900,
            'internal_canary_not_in_pinned_policy')
        return p

    def bound(self,ident):
        row=self.repository.get_state('internal_canary',ident)
        require(row and row.get('actor')=='ygor','internal_canary_missing')
        scope=row.get('scope',{})
        require(scope.get('kind')=='internal_canary' and scope.get('from')==MAILBOX
            and scope.get('max_attempts')==1 and scope.get('no_followup') is True
            and row.get('scope_hash')==payload_hash(scope)
            and row.get('expires_at',0)>time.time(),'internal_canary_scope_or_expiry')
        self.check_policy(ident,scope,row['approval_ref'])
        return row

    def current_research(self,account_id):
        if not account_id.startswith(PREFIX):return self.base.current_research(account_id)
        row=self.bound(account_id)
        evidence=self.repository.get_state('research',account_id)
        require(evidence and evidence.get('purpose')=='internal_canary'
            and evidence.get('scope_hash')==row['scope_hash'] and evidence.get('facts')==[],
            'internal_canary_evidence_changed')
        return evidence

    def require_campaign_sheet(self,campaign_id):
        if not campaign_id.startswith(PREFIX):return self.base.require_campaign_sheet(campaign_id)
        row=self.bound(campaign_id)
        config=self.campaign_registry._read(campaign_id)[0]['config']
        require(config['rules']==row['scope_hash'] and config['providers']=={'email':'gmail'}
            and config['configuration']['provider_accounts']=={'email':MAILBOX},'internal_canary_binding_changed')
        return {'purpose':'internal_canary','commercial_qualification':False,'scope_hash':row['scope_hash']}

    def identity_execution_error(self,cadence):
        ident=cadence.get('account_id','')
        if not ident.startswith(PREFIX):return self.base.identity_execution_error(cadence)
        try:
            row=self.bound(ident);scope=row['scope'];steps=cadence.get('steps',[])
            require(cadence.get('id')==ident and cadence.get('person_key')==scope['to']
                and not cadence.get('hubspot_contact_id') and len(steps)==1,'internal_canary_identity_changed')
            s=steps[0]
            require(s.get('id')=='e1' and s.get('recipient')==scope['to'] and s.get('subject')==scope['subject']
                and s.get('content')==scope['content'] and s.get('channel')=='email' and s.get('order')==1
                and not s.get('predecessor_id'),'internal_canary_envelope_changed')
            return None
        except ValueError as e:return str(e)

    def prepare(self,data,actor):
        require(actor=='ygor','internal_canary_human_authority')
        require(isinstance(data,dict) and set(data)=={'id','scope','approval_ref'},'internal_canary_fields')
        ident=data['id'];scope=data['scope']
        require(isinstance(ident,str) and re.fullmatch(PREFIX+r'[a-z0-9-]{1,60}',ident),'internal_canary_id')
        require(isinstance(scope,dict) and set(scope)=={'kind','from','to','subject','content','max_attempts','no_followup'}
            and scope['kind']=='internal_canary' and scope['from']==MAILBOX and scope['max_attempts']==1
            and type(scope['max_attempts']) is int and scope['no_followup'] is True,'internal_canary_scope')
        require(isinstance(scope['to'],str) and re.fullmatch(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',scope['to'])
            and isinstance(scope['subject'],str) and 1<=len(scope['subject'])<=200 and '\n' not in scope['subject'] and '\r' not in scope['subject']
            and isinstance(scope['content'],str) and 1<=len(scope['content'])<=2000
            and isinstance(data['approval_ref'],str) and 1<=len(data['approval_ref'])<=200,'internal_canary_content')
        policy=self.check_policy(ident,scope,data['approval_ref'])
        with self.repository.transaction():
            require(not self.repository.get_state('internal_canary',ident)
                and not self.repository.get_state('cadence',ident)
                and not self.repository.get_state('campaign_config',ident),'internal_canary_already_exists')
            digest=payload_hash(scope)
            self.repository.put_state('internal_canary',ident,{'actor':actor,'scope':deepcopy(scope),
                'scope_hash':digest,'approval_ref':data['approval_ref'],'expires_at':policy['expires_at']})
            self.repository.put_state('research',ident,{'purpose':'internal_canary','scope_hash':digest,
                'facts':[],'knowledge_source':'explicit_internal_test_authorization','commercial_qualification':False})
            config={'campaign_id':ident,'campaign_version':'1','rules_version':'1','run_id':ident,
                'platform':'thunder_os','providers':{'email':'gmail'},'rules':digest,
                'configuration':{'flow_version':'internal-canary-v1','provider_accounts':{'email':MAILBOX}}}
            registry=self.configure_campaign(config,actor,expected_version=0)
            cadence=self.create_cadence({'id':ident,'account_id':ident,'person_key':scope['to'],
                'objective':'Teste interno GTM; uma mensagem, sem follow-up ou CRM',
                'execution_context':registry['context'],'steps':[{'id':'e1','recipient':scope['to'],'channel':'email',
                    'subject':scope['subject'],'content':scope['content'],'order':1,'interval_hours':0}]},actor)
            return {'cadence':cadence,'review_hash':self.cadence_review_hash(cadence),
                'execution_confirmation':self.campaign_execution_review(cadence),'dispatch_authorized':False,
                'external_writes':0,'send_attempted':False}
