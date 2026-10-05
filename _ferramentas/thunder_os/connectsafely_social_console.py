"""Offline social copilot. Author proof must be injected; API cannot prove it.

No default resolver, transport, account or activation. Resolver is a trusted
read-only capability, not a field accepted from an untrusted preview request.
"""
import re
from urllib.parse import unquote,urlsplit
from .models import payload_hash
from .connectsafely_console import ConnectSafelyConsole,ConnectSafelyError
from .connectsafely_ledger import ConnectSafelyLedger
from .touch_ledger import TouchBlocked,STATUS_FROM_DB,toques

WIRE=frozenset({'account_id','account_public_id','action','channel','post_urn','post_url','parent_comment_urn','content','reaction_type'})
BLOCKER_HELP={
    'social_coordination_unverified':'Manter escritas sociais desativadas; implementar e verificar exclusão por post com todos os escritores legados antes de revisar este bloqueio.',
    'post_author_unverified':'Fornecer fonte confiável vinculada ao post exato para identificar seu autor; não usar autoria recebida do navegador.',
    'parent_author_unverified':'Comprovar comentário pai e identidade de seu autor em fonte confiável antes de aprovar a resposta.',
    'quota_contract_unverified':'Comprovar a feature de quota de reação no contrato do fornecedor; um nome configurado ou uma fixture não constitui comprovação.',
}

REACTIONS=frozenset({'LIKE','PRAISE','APPRECIATION','EMPATHY','INTEREST','ENTERTAINMENT'})


class ConnectSafelySocialConsole(ConnectSafelyConsole):
    def __init__(self,*args,author_resolver=None,coordinator=None,**kwargs):
        super().__init__(*args,**kwargs);self.author_resolver=author_resolver;self.coordinator=coordinator

    def activation_readiness(self,action):
        if not isinstance(action,str) or action not in {'comment','reply','react_post'}:raise ConnectSafelyError('unsupported_social_scope')
        codes=[]
        try:
            from .connectsafely_coordination_postgres import PostgresSocialCoordinator
            from .connectsafely_quarantine_resolution_postgres import PostgresResolvedSocialCoordinator
            verified=(type(self.coordinator) in (PostgresSocialCoordinator,PostgresResolvedSocialCoordinator) and self.coordinator.readiness().get('verified') is True
                and callable(getattr(self,'activation_evidence',None)) and self.activation_evidence().get('ready') is True)
        except Exception:verified=False
        if not verified:codes.append('social_coordination_unverified')
        if not callable(self.author_resolver):codes.append('post_author_unverified')
        # Feature names alone are not proof: live composition rechecks pinned approval.
        if action=='react_post' and (not verified or not getattr(getattr(self.client,'social_client',None),'reaction_quota_feature',None)):
            codes.append('quota_contract_unverified')
        return {'ready':not codes,'live_writes_supported':not codes,'blockers':[{'code':c,'action':BLOCKER_HELP[c]} for c in codes]}

    @staticmethod
    def blocker_help(code):
        return BLOCKER_HELP.get(code,'Manter a ação bloqueada e revisar a evidência antes de tentar novamente.')

    def _plan(self,body):
        internal={'recipient_public_id','recipient_profile_urn','author_binding'}
        if not isinstance(body,dict) or set(body)-(WIRE|internal) or WIRE-set(body):raise ConnectSafelyError('invalid_social_plan')
        plan={key:body[key] for key in WIRE}
        if (plan['account_id']!=self.account_id or plan['account_public_id']!=self.account_public_id
            or plan['channel']!='linkedin' or plan['action'] not in {'comment','reply','react_post'}):raise ConnectSafelyError('unsupported_social_scope')
        urn=plan['post_urn']
        if not isinstance(urn,str) or not re.fullmatch(r'urn:li:activity:[0-9]{1,30}',urn):raise ConnectSafelyError('invalid_post_urn')
        if plan['post_url']!='https://www.linkedin.com/feed/update/'+urn+'/':raise ConnectSafelyError('post_identity_mismatch')
        parent=plan['parent_comment_urn']
        if plan['action']=='reply':
            if not isinstance(parent,str) or not re.fullmatch(r'urn:li:fsd_comment:[A-Za-z0-9_:(),-]{1,400}',parent):raise ConnectSafelyError('invalid_parent_comment')
        elif parent is not None:raise ConnectSafelyError('unexpected_parent_comment')
        content=plan['content']
        if plan['action']=='react_post':
            if content!='' or plan['reaction_type'] not in REACTIONS:raise ConnectSafelyError('invalid_reaction')
        elif plan['reaction_type'] is not None or not isinstance(content,str) or not content.strip() or len(content)>3000 or any(ord(c)<32 and c not in '\n\r\t' for c in content):
            raise ConnectSafelyError('invalid_comment')
        if not callable(self.author_resolver):raise ConnectSafelyError('post_author_unverified')
        try:proof=self.author_resolver(dict(plan))
        except Exception:raise ConnectSafelyError('post_author_unverified') from None
        proof_fields={'post_urn','post_url','public_id','profile_urn'}
        if plan['action']=='reply':proof_fields|={'parent_comment_urn','parent_public_id','parent_profile_urn'}
        if (not isinstance(proof,dict) or set(proof)!=proof_fields
            or proof['post_urn']!=urn or proof['post_url']!=plan['post_url']
            or not isinstance(proof['public_id'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,150}',proof['public_id'])
            or not isinstance(proof['profile_urn'],str) or not re.fullmatch(r'urn:li:fsd_profile:[A-Za-z0-9_-]{1,200}',proof['profile_urn'])):
            raise ConnectSafelyError('post_author_unverified')
        if plan['action']=='reply' and (proof['parent_comment_urn']!=parent
            or not isinstance(proof['parent_public_id'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,150}',proof['parent_public_id'])
            or not isinstance(proof['parent_profile_urn'],str) or not re.fullmatch(r'urn:li:fsd_profile:[A-Za-z0-9_-]{1,200}',proof['parent_profile_urn'])):
            raise ConnectSafelyError('parent_author_unverified')
        enriched={**plan,'recipient_public_id':proof['public_id'],'recipient_profile_urn':proof['profile_urn'],'author_binding':dict(proof)}
        if any(key in body and body[key]!=enriched[key] for key in internal):raise ConnectSafelyError('post_author_changed')
        return enriched

    def _canonical_step(self,step,plan):
        return {**step,'action':plan['action'],'content':plan['content'],'post_urn':plan['post_urn'],'post_url':plan['post_url'],
                'secondary_recipient':('https://www.linkedin.com/in/'+plan['author_binding']['parent_public_id']) if plan['action']=='reply' else None}

    def _check_additional_targets(self,plan):
        if plan['action']=='reply':
            key=payload_hash({'account':self.account_id,'sender_urn':plan['author_binding']['parent_profile_urn']})
            if (self.repo.get_state('connectsafely_sender',key) or {}).get('has_reply') is True:
                raise ConnectSafelyError('has_reply')

    def _open_attempt(self,effect,plan,attempt_token,touch_id):
        from .connectsafely_social_client import social_request_hash
        if self.coordinator is None:raise ConnectSafelyError('social_coordination_unverified')
        return self.coordinator.issue(effect,account_id=self.account_id,action=plan['action'],post_urn=plan['post_urn'],
            attempt_token=attempt_token,request_hash=social_request_hash({key:plan[key] for key in WIRE}))

    def _retire_preflight(self,effect,attempt):
        if attempt.get('coordination_issued'):
            if self.coordinator is None:raise ConnectSafelyError('social_coordination_unverified')
            self.coordinator.retire_unconsumed(effect,attempt['attempt_token'])

    def _send(self,plan,before_dispatch,execution_context=None):
        if execution_context is None:raise ConnectSafelyError('social_coordination_unverified')
        binder=getattr(self.client,'with_social_capability',None) or getattr(self.client,'with_capability',None)
        if not callable(binder):raise ConnectSafelyError('social_coordination_unverified')
        client=binder(execution_context)
        return client.send({key:plan[key] for key in WIRE},before_dispatch=before_dispatch)

    def _reconcile(self,plan,attempted_at):
        return self.client.reconcile({key:plan[key] for key in WIRE},attempted_at)


class ConnectSafelySocialLedger(ConnectSafelyLedger):
    """Post-scoped lock for these clients + legacy lookup across all persons.

    This local lock alone does not coordinate legacy writers. Global atomicity
    requires the independently verified coordinator schema and writer inventory.
    One comment OR reply per post, one reaction per post, regardless of content,
    parent, reaction type, author metadata, account or provider.
    """
    @staticmethod
    def _scope(step,effect_key):
        if step.get('channel')!='linkedin' or step.get('action') not in {'comment','reply','react_post'}:raise TouchBlocked('unsupported_social_scope')
        urn=step.get('post_urn')
        if not isinstance(urn,str) or not re.fullmatch(r'urn:li:activity:[0-9]{1,30}',urn):raise TouchBlocked('invalid_post_urn')
        url='https://www.linkedin.com/feed/update/'+urn+'/'
        if step.get('post_url')!=url:raise TouchBlocked('post_identity_mismatch')
        return 'linkedin',('curtida' if step['action']=='react_post' else 'comentario'),toques.normalizar_pessoa('linkedin',step['recipient']),toques.normalizar_alvo('curtida',url)

    @staticmethod
    def _same_post(value,urn):
        if not isinstance(value,str) or len(value)>8192:return True
        try:
            decoded=value
            for _ in range(3):
                next_value=unquote(decoded,errors='strict')
                if next_value==decoded:break
                decoded=next_value
            if '%' in decoded:return True  # Unresolved encoding: no safe absence proof.
            parsed=urlsplit(decoded)
            if parsed.scheme not in {'https','http'} or parsed.hostname not in {'linkedin.com','www.linkedin.com'}:
                return True  # Malformed legacy target is ambiguous, never ignored.
            number=urn.rsplit(':',1)[-1]
            identity=re.search(r'(?:urn:li:activity:|activity[-:])([0-9]+)(?:[^0-9]|$)',parsed.path)
            # share/ugcPost and unknown LinkedIn paths may alias this activity.
            # Without authoritative linkage they cannot prove absence.
            return identity.group(1)==number if identity else True
        except (ValueError,UnicodeError):return True

    @staticmethod
    def _secondary(step):
        value=step.get('secondary_recipient')
        return toques.normalizar_pessoa('linkedin',value) if value else None

    def validate(self,effect,step):
        super().validate(effect,step)
        other=self._secondary(step)
        if other:
            with self._banco() as (con,mark):
                cur=con.cursor()
                for table in ('thunder_suppressions','respostas'):
                    cur.execute('SELECT pessoa_chave FROM '+table+' WHERE canal='+mark+' AND pessoa_chave='+mark+' LIMIT 1',('linkedin',other))
                    if cur.fetchone():raise TouchBlocked('has_reply_or_suppressed')
        return True

    def reserve(self,effect,step_id,step):
        channel,action,person,target=self._scope(step,effect)
        with self._banco() as (con,mark):
            # SQLite BEGIN IMMEDIATE already locks all writes. PostgreSQL also
            # takes the canonical person lock for suppression/reply coexistence.
            self._lock(con,mark,'linkedin-social',step['post_urn'])
            if mark!='?':self._lock(con,mark,channel,person)
            cur=con.cursor()
            for contact in {person,self._secondary(step)}-{None}:
                for table in ('thunder_suppressions','respostas'):
                    cur.execute('SELECT pessoa_chave FROM '+table+' WHERE canal='+mark+' AND pessoa_chave='+mark+' LIMIT 1',(channel,contact))
                    if cur.fetchone():raise TouchBlocked('has_reply_or_suppressed')
            # Lookup is intentionally across persons and providers. Activity ID
            # aliases in posts URLs are recognized; unrelated share/ugc IDs are
            # not invented as aliases without authoritative linkage.
            cur.execute('SELECT id,alvo,status,provider_id FROM toques WHERE canal='+mark+' AND acao='+mark+" AND status<>'liberado' LIMIT 10001",(channel,action))
            rows=cur.fetchall()
            if len(rows)>10000:raise TouchBlocked('legacy_lookup_incomplete')
            for row in rows:
                if row[1]==target or self._same_post(row[1],step['post_urn']):
                    return False,{'id':str(row[0]),'status':STATUS_FROM_DB[row[2]],'receipt_ref':row[3],'error_code':'canonical_social_duplicate'}
            cur.execute('INSERT INTO toques(canal,acao,pessoa_chave,alvo,status,agente,ref) VALUES ('+','.join([mark]*7)+') RETURNING id',
                        (channel,action,person,target,'reservado','thunder-connectsafely-social',step_id))
            ident=cur.fetchone()[0]
            cur.execute('INSERT INTO thunder_effect_touches(effect_key,touch_id) VALUES ('+mark+','+mark+')',(effect,ident))
            return True,{'id':str(ident),'status':'reserved'}
