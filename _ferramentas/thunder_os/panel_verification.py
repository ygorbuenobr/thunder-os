"""Stateless HubSpot-source verification via the existing tool-free Hermes child."""
from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import tempfile
from threading import BoundedSemaphore
from uuid import uuid4

from .agents import AgentJob
from .hermes_text_adapter import bounded_process_transport
from .models import payload_hash

PORTAL='51412240'


class VerificationError(ValueError):
    pass


def _clip(value,limit):
    return value[:limit] if isinstance(value,str) else ''


def _qualifiers(row):
    result={key:row[key] for key in ('delivery_verified','historical_membership_verified',
            'crm_recorded_sent','sent_evidence','response_evidence') if type(row.get(key)) is bool}
    result.update({key:_clip(row[key],150) for key in ('attribution','provider_semantics',
        'timestamp_semantics','outcome') if isinstance(row.get(key),str)})
    receipt=row.get('receipt')
    if isinstance(receipt,Mapping):
        result['receipt']={key:_clip(receipt.get(key),160) for key in ('provider','message_id','verification')}
    return result


def validate_answer(value,packet):
    if (not isinstance(value,Mapping) or set(value)!={'answer','source_ids','uncertainties'}
            or not isinstance(value['answer'],str) or not 1<=len(value['answer'].strip())<=900
            or not isinstance(value['source_ids'],list) or not 1<=len(value['source_ids'])<=6
            or any(not isinstance(v,str) or v not in packet['source_ids'] for v in value['source_ids'])
            or len(set(value['source_ids']))!=len(value['source_ids'])
            or not isinstance(value['uncertainties'],list) or len(value['uncertainties'])>3
            or any(not isinstance(v,str) or len(v)>200 for v in value['uncertainties'])):
        raise VerificationError('invalid_verification_answer')
    return dict(value)


class PanelVerification:
    def __init__(self,reader,runner=None):
        self.reader,self.runner=reader,runner
        self._slot=BoundedSemaphore(1)

    def capabilities(self):
        return {'verify':callable(self.runner),'change_route':False,'send':False}

    def contacts(self):
        try:snapshot=self.reader.snapshot()
        except Exception:raise VerificationError('hubspot_unavailable') from None
        found={}
        for campaign in snapshot.get('campaigns',[]):
            for row in campaign.get('tasks',[])+campaign.get('activities',[]):
                for ident in row.get('contact_ids',[]):
                    if isinstance(ident,str) and re.fullmatch(r'[1-9][0-9]{0,29}',ident):
                        found[ident]={'id':ident,'name':_clip(row.get('contact_names',{}).get(ident) or row.get('contact_name'),200) or 'Contato '+ident}
        return {'contacts':sorted(found.values(),key=lambda row:row['name'])[:1000],
                'complete':snapshot.get('coverage',{}).get('complete') is True and len(found)<=1000,
                'source':'hubspot','collected_at':snapshot.get('collected_at')}

    def context(self,contact_id,question):
        if not isinstance(contact_id,str) or not re.fullmatch(r'[1-9][0-9]{0,29}',contact_id):
            raise VerificationError('contact_not_confirmed')
        snapshot=self.reader.snapshot()
        if snapshot.get('ok') is False:raise VerificationError('hubspot_unavailable')
        tasks=[]
        for campaign in snapshot.get('campaigns',[]):
            tasks.extend({**task,'campaign_id':campaign['id']} for task in campaign.get('tasks',[])
                         if contact_id in task.get('contact_ids',[]))
        activity_match=any(contact_id in row.get('contact_ids',[]) for campaign in snapshot.get('campaigns',[])
                           for row in campaign.get('activities',[]))
        if not tasks and not activity_match:raise VerificationError('contact_not_confirmed')
        tasks.sort(key=lambda task:(bool(re.search(r'\bE2\b',str(task.get('title','')),re.I)) if re.search(r'\bE2\b',question,re.I) else False,str(task.get('due_at',''))),reverse=True)
        sources=[]
        for task in tasks[:6]:
            if re.fullmatch(r'[1-9][0-9]{0,29}',str(task.get('id',''))):
                sources.append({'id':'task:'+str(task['id']),'kind':'task',
                    **_qualifiers(task),
                    **{key:_clip(task.get(key),300) for key in ('title','campaign_id','due_at','status','outcome','reason','reason_code')}})
        try:timeline=self.reader.timeline(contact_id)
        except Exception:timeline={'coverage':{'complete':False},'events':[]}
        events=timeline.get('events',[])
        for event in events[-4:]:
            if isinstance(event.get('id'),str) and re.fullmatch(r'[A-Za-z0-9:_-]{1,100}',event['id']):
                sources.append({'id':event['id'],'kind':'activity',
                    **_qualifiers(event),
                    **{key:_clip(event.get(key),400) for key in ('subject','text','occurred_at','status','source','reason')}})
        sources=list({row['id']:row for row in sources}.values())
        if not sources:raise VerificationError('source_evidence_unavailable')
        limits=['Respostas no Gmail e no LinkedIn não foram verificadas nesta consulta.',
                'Atividade CRM não comprova entrega; recibos registrados são identificados separadamente.',
                'Campanha atual do contato não comprova a campanha na data de uma atividade.']
        if snapshot.get('coverage',{}).get('complete') is not True or len(tasks)>6:
            limits.append('A lista de tarefas é parcial; ausência na consulta não prova ausência de envio.')
        if timeline.get('coverage',{}).get('complete') is not True or len(events)>4:
            limits.append('A linha do tempo é parcial; ausência de evento não prova ausência de resposta.')
        source_limits=list(dict.fromkeys(_clip(v,180) for document in (snapshot,timeline)
            for v in document.get('coverage',{}).get('limitations',[]) if isinstance(v,str)))[:20]
        return {'contact_id':contact_id,'portal_id':PORTAL,'source_ids':[row['id'] for row in sources],
                'sources':sources,'collected_at':snapshot.get('collected_at'),
                'server_limits':limits,'source_limitations':source_limits,
                'coverage':{'all_contact_tasks':len(tasks),'included_tasks':min(len(tasks),6),
                    'tasks_complete':snapshot.get('coverage',{}).get('complete') is True and len(tasks)<=6,
                    'timeline_complete':timeline.get('coverage',{}).get('complete') is True and len(events)<=4,
                    'gmail_replies_verified':False,'linkedin_replies_verified':False},
                'restriction':'Only verify. No send, CRM write, pause, route change or inferred absence of reply.'}

    def verify(self,contact_id,question):
        if not isinstance(question,str) or not 1<=len(question.strip())<=600:
            raise VerificationError('invalid_question')
        if not callable(self.runner):raise VerificationError('hermes_configuration_pending')
        if not self._slot.acquire(blocking=False):raise VerificationError('verification_busy')
        try:
            try:packet=self.context(contact_id,question)
            except VerificationError:raise
            except Exception:raise VerificationError('hubspot_unavailable') from None
            if len(json.dumps(packet).encode())>18000:raise VerificationError('verification_context_too_large')
            try:answer=self.runner(packet,question.strip())
            except Exception as exc:
                if isinstance(exc,VerificationError):raise
                raise VerificationError('hermes_unavailable') from None
            answer=validate_answer(answer,packet)
            url=f'https://app.hubspot.com/contacts/{PORTAL}/record/0-1/{contact_id}'
            result={**answer,'sources':[{'id':ident,'url':url} for ident in answer['source_ids']],
                    'coverage':packet['coverage'],'read_only':True,'external_effects':False,
                    'server_limits':packet['server_limits'],
                    'contact_id':contact_id,'collected_at':packet['collected_at']}
            metadata=getattr(self.runner,'metadata',None)
            if isinstance(metadata,Mapping):result['runtime']=dict(metadata)
            return result
        finally:self._slot.release()

    def change_route(self,payload):
        raise VerificationError('action_disabled')


class HermesVerifyProcess:
    def __init__(self,*,python,hermes_root,profile,runtime_root,source_root):
        self.python,self.hermes_root,self.profile=Path(python),Path(hermes_root),Path(profile)
        self.runtime_root,self.source_root=Path(runtime_root),Path(source_root)
        self.metadata=None

    @classmethod
    def configured(cls):
        # Existing root-owned dependency lock, not an endpoint/model supplied by a query.
        if os.uname().nodename!='srv1433640':return None
        lock=Path('/etc/thunder-os/hermes-ro-runtime-lock-v2.json')
        try:
            info=lock.lstat()
            if not lock.is_file() or lock.is_symlink() or info.st_uid!=0 or info.st_mode&0o077:return None
            data=json.loads(lock.read_text())
            return cls(python=data['interpreter_path'],hermes_root=data['dependency_path'],
                profile='/root/.hermes/profiles/ygor-personal',runtime_root='/var/lib/thunder-os-agent',
                source_root=Path(__file__).resolve().parents[2])
        except (OSError,ValueError,KeyError):return None

    def __call__(self,packet,question):
        self.metadata=None
        if not self.runtime_root.is_dir() or not (self.profile/'config.yaml').is_file():
            raise VerificationError('hermes_configuration_pending')
        with tempfile.TemporaryDirectory(prefix='verify-',dir=self.runtime_root) as temporary:
            root=Path(temporary);evidence=root/'evidence.json';diagnostic=root/'diagnostic.json'
            account='hubspot:'+packet['contact_id']
            evidence.write_text(json.dumps({'account_id':account,'verification_context':packet}),encoding='utf-8')
            evidence.chmod(0o600)
            env={key:os.environ[key] for key in ('HOME','PATH','LANG','LC_ALL','TZ') if key in os.environ}
            env.update(HERMES_HOME=str(self.profile),HERMES_AUTH_READ_ONLY='1',PYTHONPATH=str(self.source_root),
                PYTHONDONTWRITEBYTECODE='1',THUNDER_EXTERNAL_EFFECTS_ENABLED='false',THUNDER_OS_EXTERNAL_EFFECTS_ENABLED='false')
            command=[str(self.python),'-m','_ferramentas.thunder_os.hermes_text_child','--mode','operation',
                '--brain',str(self.profile/'brain'),'--evidence',str(evidence),'--diagnostic',str(diagnostic),
                '--hermes-root',str(self.hermes_root),'--profile',str(self.profile)]
            job=AgentJob('verify-'+uuid4().hex,'verify',{'account_id':account,'contact_id':packet['contact_id'],
                'scope_hash':payload_hash(packet),'question':question})
            try:
                raw=bounded_process_transport(command,cwd=str(self.source_root),env=env)(job,60)
                answer=validate_answer(json.loads(raw),packet)
            except Exception:
                code='hermes_unavailable'
                if diagnostic.is_file():
                    try:
                        found=json.loads(diagnostic.read_text()).get('error_code')
                        if found in {'auth_expired','auth_unavailable','auth_readonly_violation','auth_config_invalid','auth_endpoint_blocked'}:code=found
                    except (OSError,ValueError):pass
                raise VerificationError(code) from None
            if diagnostic.is_file():
                data=json.loads(diagnostic.read_text())
                model=data.get('model')
                self.metadata={'provider':'hermes_codex_oauth','profile':'ygor-personal',
                    'model':model if isinstance(model,str) and re.fullmatch(r'[A-Za-z0-9_./:+-]{1,120}',model) else None,
                    'usage':data.get('usage') if isinstance(data.get('usage'),dict) else None}
            return answer
