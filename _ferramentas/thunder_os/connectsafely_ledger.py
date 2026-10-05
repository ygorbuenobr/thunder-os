"""Explicitly injected canonical LinkedIn ledger. No migration or credentials."""
from .touch_ledger import TouchLedger,TouchBlocked,STATUS_FROM_DB,toques
from .models import payload_hash


class ConnectSafelyLedger(TouchLedger):
    @staticmethod
    def _scope(step,effect_key):
        if step.get('channel')!='linkedin' or step.get('action') not in {'dm','invite'}:
            raise TouchBlocked('unsupported_linkedin_scope')
        person=toques.normalizar_pessoa('linkedin',step['recipient']).lower()
        if step['action']=='invite':return 'linkedin','convite',person,'convite'
        logical={'content':step['content'],'messaging_channel':step['messaging_channel']}
        return 'linkedin','mensagem',person,'thunder-os#dm:'+payload_hash(logical)

    def reserve(self,effect_key,step_id,step):
        channel,action,person,target=self._scope(step,effect_key)
        with self._banco() as (con,mark):
            self._lock(con,mark,channel,person);cur=con.cursor()
            cur.execute('SELECT reason FROM thunder_suppressions WHERE canal='+mark+' AND pessoa_chave='+mark,(channel,person))
            if cur.fetchone():raise TouchBlocked('suppressed')
            cur.execute('SELECT id FROM respostas WHERE canal='+mark+' AND pessoa_chave='+mark+' LIMIT 1',(channel,person))
            if cur.fetchone():raise TouchBlocked('has_reply')
            cur.execute('SELECT id,alvo,status,provider_id,detalhe FROM toques WHERE canal='+mark+' AND acao='+mark+' AND pessoa_chave='+mark+" AND status<>'liberado'",(channel,action,person))
            for row in cur.fetchall():
                if row[1]==target or (action=='mensagem' and not row[1].startswith('thunder-os#dm:')):
                    # Never bind a new effect to a foreign/ambiguous legacy row.
                    return False,{'id':str(row[0]),'status':STATUS_FROM_DB[row[2]],'receipt_ref':row[3],'error_code':'canonical_duplicate_or_ambiguous'}
            cur.execute('INSERT INTO toques(canal,acao,pessoa_chave,alvo,status,agente,ref) VALUES ('+','.join([mark]*7)+') RETURNING id',
                        (channel,action,person,target,'reservado','thunder-connectsafely',step_id))
            touch_id=cur.fetchone()[0]
            cur.execute('INSERT INTO thunder_effect_touches(effect_key,touch_id) VALUES ('+mark+','+mark+')',(effect_key,touch_id))
            return True,{'id':str(touch_id),'status':'reserved'}

    def validate(self,effect,step):
        self.preflight(effect,step,raise_blocked=True)
        channel,action,person,target=self._scope(step,effect)
        with self._banco() as (con,mark):
            cur=con.cursor()
            cur.execute('SELECT t.status FROM thunder_effect_touches e JOIN toques t ON t.id=e.touch_id WHERE e.effect_key='+mark+
                        ' AND t.canal='+mark+' AND t.acao='+mark+' AND t.pessoa_chave='+mark+' AND t.alvo='+mark,
                        (effect,channel,action,person,target))
            row=cur.fetchone()
            if not row or row[0]!='reservado':raise TouchBlocked('touch_not_dispatchable')
        return True

    def check_not_dispatched(self,effect,touch_id,expected_ref,step):
        """Read ownership only. The workflow journal supplies zero-dispatch proof."""
        channel,action,person,target=self._scope(step,effect)
        with self._banco() as (con,mark):
            cur=con.cursor()
            cur.execute('SELECT t.id,t.ref,t.status,t.provider_id FROM thunder_effect_touches e JOIN toques t ON t.id=e.touch_id WHERE e.effect_key='+mark+
                        ' AND t.canal='+mark+' AND t.acao='+mark+' AND t.pessoa_chave='+mark+' AND t.alvo='+mark,
                        (effect,channel,action,person,target))
            row=cur.fetchone()
            if not row or str(row[0])!=str(touch_id) or row[1]!=expected_ref or row[2]!='reservado' or row[3] is not None:
                raise TouchBlocked('reservation_not_safely_reusable')
        return True

    def resume_not_dispatched(self,effect,touch_id,expected_ref,new_ref,step):
        """CAS only the owned reservation reference, retaining touch and mapping.

        Requires the console's durable not_dispatched proof and new human review.
        Never releases the canonical target or changes a sent/uncertain touch.
        """
        channel,action,person,target=self._scope(step,effect)
        with self._banco() as (con,mark):
            self._lock(con,mark,channel,person);cur=con.cursor()
            cur.execute('UPDATE toques SET ref='+mark+',atualizado_em=CURRENT_TIMESTAMP WHERE id='+mark+
                        ' AND ref='+mark+" AND status='reservado' AND provider_id IS NULL AND canal="+mark+
                        ' AND acao='+mark+' AND pessoa_chave='+mark+' AND alvo='+mark+
                        ' AND id=(SELECT touch_id FROM thunder_effect_touches WHERE effect_key='+mark+')',
                        (new_ref,touch_id,expected_ref,channel,action,person,target,effect))
            if cur.rowcount!=1:raise TouchBlocked('reservation_not_safely_reusable')
        return True,{'id':str(touch_id),'status':'reserved'}
