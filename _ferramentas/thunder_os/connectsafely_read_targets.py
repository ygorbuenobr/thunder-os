"""Bounded provider observations for human target selection, never effect approval."""
import json
import re
import time
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit
from .connectsafely_client import ConnectSafelyClient,ConnectSafelyError,require
from .connectsafely_transport import ConnectSafelyHTTPS
from .models import payload_hash

PROFILE_ID=r'[A-Za-z0-9_.\-\u2019]{1,150}'
PROFILE=re.compile(r'https://www\.linkedin\.com/in/('+PROFILE_ID+r')/?')
POST=re.compile(r'https://www\.linkedin\.com/(?:feed/update/urn:li:activity:[0-9]{1,30}/?|posts/[A-Za-z0-9_%.-]{1,500})')
URN=re.compile(r'urn:li:activity:[0-9]{1,30}')
PERSON=re.compile(r'urn:li:fsd_profile:[A-Za-z0-9_-]{1,200}')
COMMENT=re.compile(r'urn:li:fsd_comment:[A-Za-z0-9_:(),-]{1,400}')

def normalize_target_url(value,*,profile=False):
    require(isinstance(value,str) and 0<len(value)<=2048 and all(ord(c)>32 for c in value),'invalid_target_request')
    try:parts=urlsplit(value)
    except ValueError:raise ConnectSafelyError('invalid_target_request') from None
    require(parts.scheme=='https' and parts.netloc in ('linkedin.com','www.linkedin.com'),'invalid_target_request')
    # Tracking query/fragment never participate in identity. No userinfo, port,
    # encoded hostname or arbitrary redirects are admitted by the netloc match.
    path=parts.path.rstrip('/')
    normalized='https://www.linkedin.com'+path
    if profile:
        require(PROFILE.fullmatch(normalized),'invalid_target_request')
        return normalized+'/'
    require(POST.fullmatch(normalized),'invalid_target_request')
    return normalized+'/' if path.startswith('/feed/update/') else normalized

class TargetReadHTTPS(ConnectSafelyHTTPS):
    def _route(self,method,path,query,body):
        require(isinstance(query,dict),'invalid_route')
        if method=='GET' and path=='/linkedin/accounts':return super()._route(method,path,query,body)
        if method=='GET' and path=='/linkedin/profile':
            require(body is None and set(query)=={'accountId','profileId'} and query['accountId']==self.account_id and isinstance(query['profileId'],str) and re.fullmatch(PROFILE_ID,query['profileId']),'invalid_route');return
        require(method=='POST' and query=={} and isinstance(body,dict) and body.get('accountId')==self.account_id and isinstance(body.get('postUrl'),str) and POST.fullmatch(body['postUrl']),'invalid_route')
        if path=='/linkedin/posts/scrape':
            require(set(body)=={'accountId','postUrl','useCache','forceRefresh','maxProxyRetries'} and body['useCache'] is True and body['forceRefresh'] is False and type(body['maxProxyRetries']) is int and body['maxProxyRetries']==0,'invalid_route');return
        require(path=='/linkedin/posts/comments' and set(body)=={'accountId','postUrl','commentCount','start'} and type(body['commentCount']) is int and body['commentCount']==10 and type(body['start']) is int and body['start']==0,'invalid_route')


def _text(value,limit=200):
    require(isinstance(value,str) and len(value)<=limit,'provider_schema_invalid');return value

class ReadTargetResolver:
    def __init__(self,repository,client,*,clock=time.time):self.repo,self.client,self.clock=repository,client,clock
    def _quota_key(self):
        return payload_hash({'account':self.client.account_id,'window':'rolling24h'})
    def _quota(self):
        row=self.repo.get_state('connectsafely_profile_read_quota',self._quota_key()) or {}
        now=self.clock();reservations=row.get('reservations',{})
        require(isinstance(reservations,dict) and len(reservations)<=120,'provider_schema_invalid')
        require(all(isinstance(k,str) and type(v) in (int,float) and 0<v<=now for k,v in reservations.items()),'provider_schema_invalid')
        return {'reservations':{k:v for k,v in reservations.items() if v>now-86400},'blocked_until':row.get('blocked_until',0)}
    def _profile(self,url):
        match=PROFILE.fullmatch(url) if isinstance(url,str) else None;require(match is not None,'profile_target_unverified')
        public=match.group(1);c=self.client
        with self.repo.transaction():
            quota=self._quota();require(quota['blocked_until']<=self.clock(),'profile_quota_exhausted')
            profiles=quota['reservations'];require(public in profiles or len(profiles)<120,'profile_quota_exhausted')
            # Reserve before IO, retaining timeout/unknown calls. Refresh an
            # existing identity's timestamp so it cannot age out after a repeat.
            self.repo.put_state('connectsafely_profile_read_quota',self._quota_key(),{'reservations':{**profiles,public:self.clock()},'blocked_until':quota['blocked_until']})
        data=c._call('GET','/linkedin/profile',{'accountId':c.account_id,'profileId':public})
        require(data.get('success') is True and data.get('accountId')==c.account_id and data.get('profileId')==public,'account_identity_mismatch')
        profile=data.get('profile');require(isinstance(profile,dict) and profile.get('publicIdentifier')==public and isinstance(profile.get('entityUrn'),str) and PERSON.fullmatch(profile['entityUrn']),'profile_target_unverified')
        require(type(data.get('cached')) is bool,'provider_schema_invalid')
        names=[_text(profile[k]) for k in ('firstName','lastName') if k in profile]
        return {'public_id':public,'profile_urn':profile['entityUrn'],'label':' '.join(names) or public,'cached':data['cached']}
    def _save(self,payload):
        identity=uuid4().hex
        self.repo.append_state('connectsafely_target_observation',identity,{'observation':payload,'source_hash':payload_hash(payload)})
        return identity
    def _load(self,identity):
        require(isinstance(identity,str) and re.fullmatch(r'[a-f0-9]{32}',identity),'target_observation_unverified')
        row=self.repo.get_state('connectsafely_target_observation',identity) or {};value=row.get('observation')
        require(isinstance(value,dict) and row.get('source_hash')==payload_hash(value) and value.get('account_id')==self.client.account_id and value.get('expires_at',0)>self.clock(),'target_observation_unverified')
        return value
    def author(self,plan):
        key=payload_hash({'account':self.client.account_id,'post':plan['post_urn'],'parent':plan.get('parent_comment_urn')})
        ref=self.repo.get_state('connectsafely_target_binding',key) or {};observation=self._load(ref.get('observation_id'))
        proof=ref.get('proof');require(isinstance(proof,dict) and ref.get('source_hash')==payload_hash(proof) and proof.get('post_urn')==observation.get('post_urn') and proof.get('post_url')==plan['post_url'],'post_author_unverified')
        require(proof in observation.get('proofs',[]),'post_author_unverified');return proof
    def _bind(self,identity,proof):
        key=payload_hash({'account':self.client.account_id,'post':proof['post_urn'],'parent':proof.get('parent_comment_urn')})
        self.repo.put_state('connectsafely_target_binding',key,{'observation_id':identity,'proof':proof,'source_hash':payload_hash(proof)})
    def __call__(self,body):
        try:return self.resolve(body)
        except ConnectSafelyError as exc:
            if str(exc)=='provider_rate_limited':
                with self.repo.transaction():
                    old=self._quota()
                    self.repo.put_state('connectsafely_profile_read_quota',self._quota_key(),{'reservations':old['reservations'],'blocked_until':self.clock()+86400})
            allowed={'profile_quota_exhausted','provider_unauthorized','provider_forbidden','provider_rate_limited','provider_not_found','provider_outcome_unknown','profile_target_unverified','post_target_unverified','comment_target_unverified','target_observation_unverified','account_identity_mismatch','invalid_target_request','provider_schema_invalid'}
            return {'resolved':False,'status':'blocked','read_only':True,'actions_available':False,'error_code':str(exc) if str(exc) in allowed else 'target_evidence_unverified'}
        except Exception:return {'resolved':False,'status':'blocked','read_only':True,'actions_available':False,'error_code':'target_evidence_unverified'}
    def resolve(self,body):
        require(isinstance(body,dict) and not set(body)-{'action','profile_url','post_url','observation_id','selected_comment_id','parent_comment_url'} and body.get('action') in {'dm','invite','comment','reply','react_post'},'invalid_target_request')
        body=dict(body)
        if 'profile_url' in body:body['profile_url']=normalize_target_url(body['profile_url'],profile=True)
        if 'post_url' in body:body['post_url']=normalize_target_url(body['post_url'])
        c=self.client;action=body['action'];base={'account_id':c.account_id,'account_public_id':c.expected_public_id,'channel':'linkedin','action':action}
        result={'resolved':True,'read_only':True,'actions_available':False,'source':'provider_observation'}
        if 'observation_id' in body:
            require(action=='reply' and set(body)=={'action','post_url','observation_id','selected_comment_id'},'invalid_target_request')
            value=self._load(body['observation_id']);require(value.get('requested_url')==body['post_url'],'post_target_unverified')
            matches=[p for p in value['proofs'] if p.get('parent_comment_urn')==body['selected_comment_id']]
            require(len(matches)==1,'comment_target_unverified');proof=matches[0];self._bind(body['observation_id'],proof)
            return {**result,'observation_id':body['observation_id'],'target':{**base,'post_url':proof['post_url'],'post_urn':proof['post_urn'],'parent_comment_urn':proof['parent_comment_urn']},'preview':{'label':value['label'],'content':value['content'],'content_available':True,'parent_author_public_id':proof['parent_public_id']}}
        require('selected_comment_id' not in body and 'parent_comment_url' not in body,'invalid_target_request')
        if action in {'dm','invite'}:
            require(set(body)=={'action','profile_url'} and isinstance(body.get('profile_url'),str) and PROFILE.fullmatch(body['profile_url']),'invalid_target_request')
        else:require(set(body)=={'action','post_url'} and isinstance(body.get('post_url'),str) and POST.fullmatch(body['post_url']),'invalid_target_request')
        quota=self._quota()
        require(quota['blocked_until']<=self.clock() and len(quota['reservations'])<120,'profile_quota_exhausted')
        # Exactly one account check; a run makes at most four provider requests.
        c.verify_identity()
        if action in {'dm','invite'}:
            require(set(body)=={'action','profile_url'},'invalid_target_request');person=self._profile(body['profile_url'])
            return {**result,'target':{**base,'recipient_public_id':person['public_id'],'recipient_profile_urn':person['profile_urn'],'conversation_urn':None,'messaging_channel':'linkedin_inbox'},'preview':{'label':person['label'],'profile_url':body['profile_url'],'cached':person['cached'],'relationship_freshness':'not_verified_for_dispatch'}}
        require(set(body)=={'action','post_url'} and isinstance(body['post_url'],str) and POST.fullmatch(body['post_url']),'invalid_target_request')
        url=body['post_url'];data=c._call('POST','/linkedin/posts/scrape',body={'accountId':c.account_id,'postUrl':url,'useCache':True,'forceRefresh':False,'maxProxyRetries':0})
        require(data.get('success') is True and data.get('originalUrl')==url and data.get('usingAuthenticatedScraping') is True,'post_target_unverified')
        item=data.get('data');require(isinstance(item,dict) and isinstance(item.get('activityUrn'),str) and URN.fullmatch(item['activityUrn']),'post_target_unverified')
        if '/feed/update/' in url:require(url.rstrip('/').endswith(item['activityUrn']),'post_target_unverified')
        canonical='https://www.linkedin.com/feed/update/'+item['activityUrn']+'/'
        # API explicitly distinguishes originalUrl, redirected finalUrl and the
        # canonical data.url. Only the requested URL or this exact returned
        # activity on LinkedIn is admissible; never derive an ID from /posts/.
        allowed_urls={url,canonical,canonical.rstrip('/')}
        require(data.get('finalUrl') in allowed_urls and item.get('url') in allowed_urls and isinstance(item.get('author'),dict),'post_target_unverified')
        author=self._profile(normalize_target_url(item['author'].get('profileUrl'),profile=True));content=_text(item.get('content'),10000)
        canonical='https://www.linkedin.com/feed/update/'+item['activityUrn']+'/'
        proof={'post_urn':item['activityUrn'],'post_url':canonical,'public_id':author['public_id'],'profile_urn':author['profile_urn']}
        value={'account_id':c.account_id,'requested_url':url,'post_urn':item['activityUrn'],'label':'Publicação de '+author['label'],'content':content,'proofs':[proof],'expires_at':self.clock()+300,'provider_freshness':'cached_or_unknown'}
        comments=[];complete=None
        if action=='reply':
            observed=c._call('POST','/linkedin/posts/comments',body={'accountId':c.account_id,'postUrl':canonical,'commentCount':10,'start':0})
            require(observed.get('success') is True and observed.get('accountId')==c.account_id and observed.get('postUrl')==canonical and isinstance(observed.get('postDetails'),dict) and observed['postDetails'].get('activityUrn')==item['activityUrn'],'post_target_unverified')
            rows=observed.get('comments');require(isinstance(rows,list) and len(rows)<=10,'provider_schema_invalid');seen=set()
            for row in rows:
                require(isinstance(row,dict) and isinstance(row.get('commentId'),str) and COMMENT.fullmatch(row['commentId']) and row['commentId'] not in seen,'comment_target_unverified');seen.add(row['commentId'])
                urn=row.get('commenterUrn');public=row.get('publicIdentifier');require(isinstance(urn,str) and PERSON.fullmatch(urn) and isinstance(public,str) and re.fullmatch(PROFILE_ID,public),'comment_target_unverified')
                comments.append({'id':row['commentId'],'text':_text(row.get('commentText'),8000),'author_label':_text(row.get('authorName',public))})
                value['proofs'].append({**proof,'parent_comment_urn':row['commentId'],'parent_public_id':public,'parent_profile_urn':urn})
            page=observed.get('pagination');require(isinstance(page,dict) and type(page.get('hasNextPage')) is bool,'provider_schema_invalid');complete=page['hasNextPage'] is False
        identity=self._save(value)
        if action=='reply':return {**result,'resolved':False,'status':'selection_required','observation_id':identity,'comments':comments,'window_complete':complete,'preview':{'label':value['label'],'content':content,'content_available':True},'provider_freshness':'cached_or_unknown'}
        self._bind(identity,proof)
        return {**result,'observation_id':identity,'target':{**base,'post_urn':proof['post_urn'],'post_url':canonical,'parent_comment_urn':None},'preview':{'label':value['label'],'content':content,'content_available':True},'provider_freshness':'cached_or_unknown'}


def build_read_resolver(repository,config_path='/var/lib/thunder-os-inbound-monitor/config.json'):
    """Reuse only the explicitly authorized existing monitor config; no env search."""
    from .connectsafely_monitor import validate_config
    from .connectsafely_live import credential
    path=Path(config_path)
    if not path.is_file() or path.is_symlink():return None
    cfg=json.loads(path.read_text());validate_config(cfg)
    require(cfg['version']==2 and Path(cfg['repository']).resolve()==Path(repository.path).resolve(),'monitor_config_invalid')
    client=ConnectSafelyClient(cfg['account_id'],cfg['account_public_id'],self_profile_id=cfg['self_profile_id'],request=TargetReadHTTPS(cfg['account_id'],credential(cfg['api_credential_file'])))
    return ReadTargetResolver(repository,client)
