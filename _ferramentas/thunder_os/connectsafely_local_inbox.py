"""Authenticated UI projection of existing observations; never contacts a provider.

Source hashes detect inconsistent stored records, not cryptographic provenance.
A bounded read-only SQLite snapshot avoids migrations and PRAGMA WAL writes.
"""
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
from .models import payload_hash

KINDS={'inbox_account_message':1000,'inbox_account_revision':200,
       'inbox_account_display':100,'inbox_account_thread':100,'inbox_account_scan':2}


def _check(condition):
    if not condition:raise ValueError('local_observation_unverified')


def _snapshot(repository):
    con=sqlite3.connect(Path(repository.path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        con.execute('PRAGMA query_only=ON');con.execute('BEGIN')
        rows={}
        for kind,limit in KINDS.items():
            found=con.execute('SELECT entity_id,payload FROM thunder_state WHERE kind=? LIMIT ?', (kind,limit+1)).fetchall()
            _check(len(found)<=limit)
            rows[kind]={key:json.loads(value) for key,value in found}
        row=con.execute("SELECT payload FROM thunder_state WHERE kind='connectsafely_account_monitor' AND entity_id='global'").fetchone()
        monitor=json.loads(row[0]) if row else {}
        return rows,monitor
    finally:con.close()


def _message(key,value,revision=False):
    m=value.get('message');_check(isinstance(m,dict) and value.get('source_hash')==payload_hash(m))
    for field in ('account_id','conversation_urn','message_urn','sender_profile_id'):
        _check(isinstance(m.get(field),str) and 0<len(m[field])<=2048)
    expected=payload_hash({'account':m['account_id'],'thread':m['conversation_urn'],'message':m['message_urn']})
    _check(key==(expected+':'+payload_hash(m) if revision else expected))
    if revision:_check(value.get('original_id')==expected and value.get('state')=='review_required')
    _check(type(m.get('sent_at_ms')) is int and m['sent_at_ms']>0 and type(m.get('outgoing')) is bool)
    _check(type(m.get('body_complete')) is bool and (m.get('content') is None or isinstance(m['content'],str) and len(m['content'])<=8000))
    _check(m.get('source')=='observed_provider')
    return m


def local_inbox(repository):
    result={'source':'persisted_monitor','read_only':True,'actions_available':False,
            'account':None,'conversations':[],'contacts':[],'state':'unavailable',
            'coverage':'unknown','updated_at':None,'provider_freshness':'cached_or_unknown',
            'contact_resolution_available':False,'contact_resolution_scope':'display_only'}
    try:
        rows,monitor=_snapshot(repository)
        _check(isinstance(monitor,dict))
        public=monitor.get('account_public_id')
        _check(public is None or isinstance(public,str) and bool(re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',public)))
        messages={key:_message(key,value) for key,value in rows['inbox_account_message'].items()}
        revisions={key:_message(key,value,True) for key,value in rows['inbox_account_revision'].items()}
        accounts={m['account_id'] for m in [*messages.values(),*revisions.values()]}
        displays={}
        for key,value in rows['inbox_account_display'].items():
            d=value.get('display');_check(isinstance(d,dict) and value.get('source_hash')==payload_hash(d))
            _check(d.get('account_public_id')==public and isinstance(d.get('account_id'),str))
            _check(key==payload_hash({'account':d['account_id'],'thread':d.get('conversation_urn')}))
            _check(d.get('source')=='observed_provider' and isinstance(d.get('identities'),list) and all(isinstance(x,str) for x in d['identities']))
            _check(isinstance(d.get('participants'),list) and len(d['participants'])<=20)
            accounts.add(d['account_id']);displays[key]=d
        _check(len(accounts)<=1)
        if not accounts:return result
        account=next(iter(accounts));_check(public is not None)
        scan_key=payload_hash({'account':account});scans=rows['inbox_account_scan']
        _check(set(scans)=={scan_key})
        scan=scans[scan_key];pending=scan.get('pending',{});_check(isinstance(pending,dict) and len(pending)<=50)
        threads={}
        for mid,m in messages.items():
            key=payload_hash({'account':account,'thread':m['conversation_urn']})
            threads.setdefault(key,[]).append((mid,m))
        for key in displays:threads.setdefault(key,[])
        projected=[];contacts={}
        for key,items in threads.items():
            display=displays.get(key);binding=pending.get(key)
            identities=display.get('identities') if display else None
            # Historic completed observations lack participant bindings: expose an
            # honest placeholder, not private content whose scope is unverifiable.
            verified=isinstance(identities,list) and len(set(identities))==2 and all(isinstance(x,str) for x in identities)
            title='Conversa observada';contact=None
            if display and verified:
                owner=display.get('owner_identity');_check(owner in identities)
                peers=[p for p in display['participants'] if isinstance(p,dict) and p.get('identity')!=owner]
                _check(all(p.get('identity') in identities for p in peers))
                names={p['name'] for p in peers if isinstance(p.get('name'),str) and 0<len(p['name'])<=200 and all(ord(x)>=32 for x in p['name'])}
                urls={p['profile_url'] for p in peers if isinstance(p.get('profile_url'),str)}
                _check(len(names)<=1 and len(urls)<=1)
                name=next(iter(names),None);url=next(iter(urls),None)
                if url:
                    u=urlsplit(url);_check(u.scheme=='https' and u.netloc in ('linkedin.com','www.linkedin.com') and bool(re.fullmatch(r'/in/[A-Za-z0-9_%.-]+/?',u.path)) and not u.query and not u.fragment)
                if name:title=name
                if name or url:
                    peer=next(x for x in identities if x!=owner);cid=payload_hash({'account':account,'person':peer})
                    contact={'id':cid,'name':name,'profile_url':url,'source':'observed_provider','actions_available':False}
                    contacts[cid]=contact
            shown=[]
            for mid,m in sorted(items,key=lambda item:item[1]['sent_at_ms'])[-50:]:
                if not verified:continue
                _check(m['sender_profile_id'] in identities)
                if display:_check((m['sender_profile_id']==display['owner_identity'])==m['outgoing'])
                revised=any(v.get('original_id')==mid for v in rows['inbox_account_revision'].values())
                shown.append({'id':mid,'text':m.get('content'),'direction':'outgoing' if m['outgoing'] else 'incoming',
                              'sent_at':m['sent_at_ms']/1000,'body_complete':m['body_complete'],'revision_pending':revised})
            state=rows['inbox_account_thread'].get(key,{})
            projected.append({'id':key,'title':title,'contact_id':contact['id'] if contact else None,
                'messages':shown,'identity_verified':verified,'pending':key in pending,
                'coverage':'complete' if verified and state.get('complete') is True else 'partial',
                'last_activity_at':max([m['sent_at_ms']/1000 for _,m in items]+[display.get('activity_ms',0)/1000 if display else 0]),
                'messages_truncated':len(items)>50,'actions_available':False})
        _check(len(projected)<=100)
        stamp=monitor.get('last_finished_at');_check(stamp is None or type(stamp) in (int,float))
        result.update(account={'label':public,'public_id':public},conversations=sorted(projected,key=lambda x:x['last_activity_at'],reverse=True),
            contacts=list(contacts.values()),state='available',coverage='partial' if pending or scan.get('coverage')!='complete_listing' or any(x['coverage']!='complete' for x in projected) else 'complete_listing',
            updated_at=stamp,contacts_available=bool(contacts))
        return result
    except (ValueError,TypeError,KeyError,AttributeError,StopIteration,sqlite3.Error):
        return {**result,'state':'blocked','error_code':'local_observation_unverified'}
