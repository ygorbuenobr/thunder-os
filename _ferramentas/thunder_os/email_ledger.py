"""Execute-only email ledger, dedicated connection; no general DB URL or migrations."""
import json
import os
import re
import stat
from pathlib import Path
from .touch_ledger import TouchBlocked
MAILBOX_ID='452c6677-5ead-41d8-b036-6ef63945752d'
ROLE='thunder_seqd_email'
STATES={'reservado':'reserved','feito':'sent','incerto':'uncertain','blocked':'blocked'}


def _credential(value):
    if (not isinstance(value,dict) or set(value)!={'host','port','database','user','password'}
        or value['host']!='127.0.0.1' or value['port']!=54322 or type(value['port']) is not int
        or value['user']!=ROLE or value['database']!='thunder_gtm'
        or not isinstance(value['password'],str) or not 16<=len(value['password'])<=4096 or any(ord(c)<32 for c in value['password'])):
        raise TouchBlocked('email_ledger_credential_unavailable')
    return dict(value)


def credential_file():
    explicit=os.environ.get('THUNDER_OS_SEQD_EMAIL_LEDGER_CREDENTIAL_FILE','')
    path=Path(explicit) if explicit else Path(os.environ.get('CREDENTIALS_DIRECTORY',''))/'seqd_email_ledger'
    try:
        if not path.is_absolute() or path.name!='seqd_email_ledger': raise ValueError()
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'r') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o600 or not 1<=info.st_size<=8192: raise ValueError()
            return _credential(json.loads(stream.read(8193)))
    except Exception: raise TouchBlocked('email_ledger_credential_unavailable') from None


class SEQDEmailLedger:
    def __init__(self,credential_loader=credential_file,*,connect=None):
        self._loader,self._connect=credential_loader,connect

    def _call(self,operation,args=()):
        sql={'validate':'SELECT thunder_email_ledger.validate_email(%s)',
             'readiness':'SELECT thunder_email_ledger.readiness()',
             'reserve':'SELECT thunder_email_ledger.reserve_email(%s,%s,%s,%s,%s)',
             'finish':'SELECT thunder_email_ledger.finish_email(%s,%s,%s)'}
        connection=None
        try:
            config=_credential(self._loader());connect=self._connect
            if connect is None:
                import psycopg2
                connect=psycopg2.connect
            connection=connect(host=config['host'],port=config['port'],dbname=config['database'],user=ROLE,
                password=config['password'],connect_timeout=5,
                options='-c default_transaction_read_only=off -c statement_timeout=5000 -c lock_timeout=3000',
                application_name='thunder-seqd-email-ledger')
            with connection.cursor() as cursor:
                cursor.execute(sql[operation],args);result=cursor.fetchone()[0]
            connection.commit()
            if not isinstance(result,dict): raise ValueError()
            return result
        except Exception:
            if connection is not None:
                try: connection.rollback()
                except Exception: pass
            raise TouchBlocked('email_ledger_operation_unavailable') from None
        finally:
            if connection is not None:
                try: connection.close()
                except Exception: pass

    def readiness(self):
        try:
            value=self._call('readiness')
            return None if value.get('ready') is True and value.get('mailbox')==MAILBOX_ID else 'canonical_ledger_readonly'
        except TouchBlocked: return 'canonical_ledger_unavailable'

    def validate(self,effect):
        self._effect(effect)
        result=self._call('validate',(effect,))
        if result.get('allowed') is not True or result.get('mailbox')!=MAILBOX_ID:
            raise TouchBlocked('email_ledger_contact_blocked')
        return True

    @staticmethod
    def _effect(value):
        if not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{64}',value): raise TouchBlocked('email_ledger_invalid_scope')

    @staticmethod
    def _stored(value):
        if value.get('status') not in STATES: raise TouchBlocked('email_ledger_invalid_result')
        return {**value,'status':STATES[value['status']]}

    def reserve(self,effect,preview,step):
        self._effect(effect)
        if (not isinstance(step,dict) or step.get('channel')!='email' or step.get('mailbox_id')!=MAILBOX_ID
            or not isinstance(preview,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',preview)):
            raise TouchBlocked('email_ledger_invalid_scope')
        self._effect(step.get('approval_hash'))
        result=self._call('reserve',(effect,preview,step['approval_hash'],step['recipient'],step['subject']))
        if type(result.get('created')) is not bool: raise TouchBlocked('email_ledger_invalid_result')
        return result['created'],self._stored(result)

    def finish(self,effect,status,receipt_ref=None,error_code=None):
        self._effect(effect)
        if status not in {'sent','uncertain'}: raise TouchBlocked('email_ledger_invalid_finish')
        if status=='sent' and (not isinstance(receipt_ref,str) or not re.fullmatch(r'gmail:[A-Za-z0-9_-]{1,200}',receipt_ref)):
            raise TouchBlocked('email_ledger_invalid_finish')
        return self._stored(self._call('finish',(effect,'feito' if status=='sent' else 'incerto',receipt_ref if status=='sent' else None)))
