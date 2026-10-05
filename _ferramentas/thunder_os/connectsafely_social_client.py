"""Offline-first social port. No automatic credentials, retries, or channel fallback.

POST /posts/comments is a documented read. Only comment/reply/react endpoints
are dispatches. Success echoes without a verifiable effect ID remain uncertain.
Own history does not distinguish a top-level comment from a reply or prove a
reply's parent. Reactions have no durable receipt ID in the public schema.
Matching observations therefore remain uncertain, never confirmed dispatches.
"""
from datetime import datetime
from hashlib import sha256
import json
import math
import re
from urllib.parse import urlsplit
from .connectsafely_client import ConnectSafelyClient, ConnectSafelyError, require, select, public_id

REACTIONS=frozenset(('LIKE','PRAISE','APPRECIATION','EMPATHY','INTEREST','ENTERTAINMENT'))
POST=re.compile(r'urn:li:activity:[0-9]{1,25}')
COMMENT=re.compile(r'urn:li:fsd_comment:\(([0-9]{1,25}),(urn:li:(?:activity|ugcPost|share):[0-9]{1,25})\)')


def post_identity(url):
    require(isinstance(url,str) and len(url)<=2048,'invalid_post_url')
    parsed=urlsplit(url)
    require(parsed.scheme=='https' and parsed.netloc=='www.linkedin.com' and not parsed.query and not parsed.fragment,'invalid_post_url')
    found=re.fullmatch(r'/feed/update/(urn:li:activity:[0-9]{1,25})/?',parsed.path)
    if not found:
        found=re.fullmatch(r'/posts/[A-Za-z0-9_-]+-activity-([0-9]{1,25})-[A-Za-z0-9_-]+/?',parsed.path)
        require(found is not None,'invalid_post_url')
        return 'urn:li:activity:'+found.group(1)
    return found.group(1)


def same_profile(left,right):
    """Only the documented bare ACo member-id / fsd_profile equivalence."""
    def normalized(value):
        if isinstance(value,str) and re.fullmatch(r'ACo[A-Za-z0-9_-]+',value):
            return 'urn:li:fsd_profile:'+value
        return value
    return left is not None and right is not None and normalized(left)==normalized(right)


def social_wire_request(plan):
    """Canonical approved wire snapshot, also bound into the coordination proof."""
    body={'accountId':plan['account_id']}
    if plan['action']=='comment':
        path='/linkedin/posts/comment';body.update(postUrl=plan['post_url'],comment=plan['content'],tagPostAuthor=False)
    elif plan['action']=='reply':
        path='/linkedin/reply';body.update(commentId=plan['parent_comment_urn'],reply=plan['content'])
    elif plan['action']=='react_post':
        path='/linkedin/posts/react';body.update(postUrl=plan['post_url'],threadUrn=plan['post_urn'],reactionType=plan['reaction_type'])
    else:raise ConnectSafelyError('unsupported_action')
    return path,body


def wire_request_hash(path,body):
    return sha256(json.dumps({'method':'POST','path':path,'body':body},sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()


def social_request_hash(plan):
    return wire_request_hash(*social_wire_request(plan))


class ConnectSafelySocialClient(ConnectSafelyClient):
    def __init__(self,*args,reaction_quota_feature=None,**kwargs):
        super().__init__(*args,**kwargs)
        require(reaction_quota_feature is None or isinstance(reaction_quota_feature,str) and re.fullmatch(r'[A-Za-z0-9_]{1,80}',reaction_quota_feature),'quota_configuration_invalid')
        self.reaction_quota_feature=reaction_quota_feature

    def with_capability(self,capability):
        from copy import copy
        from .connectsafely_social_transport import ConnectSafelySocialHTTPS
        require(isinstance(self.request,ConnectSafelySocialHTTPS),'social_coordination_unverified')
        cloned=copy(self);cloned.request=self.request.with_capability(capability)
        return cloned

    def _plan(self,plan):
        keys={'account_id','account_public_id','action','channel','post_urn','post_url','parent_comment_urn','content','reaction_type'}
        require(isinstance(plan,dict) and set(plan)==keys,'invalid_plan')
        require(plan['account_id']==self.account_id and plan['account_public_id']==self.expected_public_id,'account_identity_mismatch')
        require(plan['channel']=='linkedin' and plan['action'] in ('comment','reply','react_post'),'unsupported_action')
        require(isinstance(plan['post_urn'],str) and POST.fullmatch(plan['post_urn']) and post_identity(plan['post_url'])==plan['post_urn'],'post_identity_mismatch')
        # The approved wire URL is canonical; no tracking or author-substitution.
        require(plan['post_url']=='https://www.linkedin.com/feed/update/'+plan['post_urn']+'/','invalid_post_url')
        if plan['action']=='reply':require(isinstance(plan['parent_comment_urn'],str) and COMMENT.fullmatch(plan['parent_comment_urn']),'invalid_parent_comment')
        else:require(plan['parent_comment_urn'] is None,'invalid_parent_comment')
        content=plan['content']
        require(isinstance(content,str) and '\x00' not in content and not any(0xD800<=ord(c)<=0xDFFF for c in content),'invalid_content')
        # No structured tagging or interpolation in this restricted personal port.
        require('@' not in content and '{{' not in content and '}}' not in content,'tagging_unsupported')
        if plan['action']=='react_post':require(content=='' and isinstance(plan['reaction_type'],str) and plan['reaction_type'] in REACTIONS,'invalid_reaction')
        else:require(bool(content.strip()) and len(content)<=1250 and plan['reaction_type'] is None,'invalid_content')

    @staticmethod
    def _time(value):
        require(type(value) in (int,float) and math.isfinite(value) and value>0,'history_evidence_incomplete')
        return value/1000 # These history schemas explicitly document epoch milliseconds.

    def history(self,kind):
        require(kind in ('comments','reactions'),'unsupported_action');self.verify_identity()
        require(self.self_profile_id is not None,'owner_identity_unconfigured')
        output=[];seen=set();token=None
        for _ in range(3):
            query={'count':10}
            if token is not None:query['paginationToken']=token
            data=self._call('GET','/linkedin/account/'+self.account_id+'/'+kind,query)
            self._scope(data)
            require(data.get('accountId')==self.account_id and same_profile(data.get('profileId'),self.self_profile_id),'account_identity_mismatch')
            require('paginationToken' in data,'pagination_unverified')
            token=data['paginationToken']
            require(token is None or isinstance(token,str) and 0<len(token)<=2048,'pagination_unverified')
            rows=data.get(kind)
            require(isinstance(rows,list) and len(rows)<=10 and type(data.get('count')) is int and data['count']==len(rows),'provider_schema_invalid')
            fields={k:str for k in (('activityUrn','postActivityUrn','commentUrn','commentText','postUrl','type') if kind=='comments' else ('activityUrn','postUrl','reactionType','type'))}
            for row in rows:
                item=select(row,fields)
                if row.get('timestamp') is not None:
                    self._time(row['timestamp']);item['timestamp']=row['timestamp']
                require(item.get('type')==('comment' if kind=='comments' else 'reaction'),'history_evidence_incomplete')
                output.append(item)
            if token is None:return output
            require(token not in seen,'pagination_unverified');seen.add(token)
        raise ConnectSafelyError('history_incomplete')

    def _target(self,row,kind):
        field='postActivityUrn' if kind=='comments' else 'activityUrn'
        value=row.get(field)
        if isinstance(value,str) and re.fullmatch('[0-9]{1,25}',value):value='urn:li:activity:'+value
        require(isinstance(value,str) and POST.fullmatch(value),'history_evidence_incomplete')
        if row.get('postUrl') is not None:require(post_identity(row['postUrl'])==value,'post_identity_mismatch')
        return value

    def _post_comments(self,plan):
        # Documented READ endpoint; not a side-effect dispatch despite POST method.
        rows=[];seen=set();token=None;start=0
        for _ in range(3):
            body={'accountId':self.account_id,'postUrl':plan['post_url'],'commentCount':10,'start':start}
            if token is not None:body['paginationToken']=token
            data=self._call('POST','/linkedin/posts/comments',body=body)
            self._scope(data)
            require(data.get('accountId')==self.account_id and post_identity(data.get('postUrl'))==plan['post_urn'],'post_identity_mismatch')
            details=data.get('postDetails');require(isinstance(details,dict) and details.get('activityUrn')==plan['post_urn'],'post_identity_mismatch')
            parents={plan['post_urn']}
            for key,prefix in [('ugcPostUrn','urn:li:ugcPost:'),('shareUrn','urn:li:share:')]:
                value=details.get(key)
                require(value is None or isinstance(value,str) and re.fullmatch(re.escape(prefix)+r'[0-9]{1,25}',value),'post_identity_mismatch')
                if value:parents.add(value)
            page=data.get('comments');pagination=data.get('pagination')
            require(isinstance(page,list) and len(page)<=10 and isinstance(pagination,dict) and type(pagination.get('hasNextPage')) is bool,'pagination_unverified')
            require(type(pagination.get('count')) is int and pagination['count']==len(page),'pagination_unverified')
            if 'start' in pagination:require(type(pagination['start']) is int and pagination['start']==start,'pagination_unverified')
            if 'total' in pagination:require(type(pagination['total']) is int and pagination['total']>=len(rows)+len(page),'pagination_unverified')
            for row in page:
                item=select(row,{'commentId':str,'commentText':str,'commenterUrn':str,'commenterProfileId':str,'publicIdentifier':str})
                author=item.get('commenterUrn');member=item.get('commenterProfileId');slug=item.get('publicIdentifier')
                require(author is None or isinstance(author,str) and re.fullmatch(r'urn:li:fsd_profile:[A-Za-z0-9_-]+',author),'comment_author_unverified')
                require(slug is None or public_id(slug),'comment_author_unverified')
                require(member is None or isinstance(member,str) and re.fullmatch(r'ACo[A-Za-z0-9_-]+',member),'comment_author_unverified')
                require(author is not None or member is not None or slug is not None,'comment_author_unverified')
                if author is not None and member is not None:require(same_profile(author,member),'comment_author_unverified')
                match=COMMENT.fullmatch(item.get('commentId',''))
                require(match and match.group(2) in parents,'parent_comment_unverified');rows.append(item)
            if pagination['hasNextPage'] is False:
                require('nextPaginationToken' in pagination and pagination['nextPaginationToken'] is None,'pagination_unverified')
                if 'total' in pagination:require(pagination['total']==len(rows),'pagination_unverified')
                if 'nextStart' in pagination:require(pagination['nextStart'] is None,'pagination_unverified')
                require(len({row['commentId'] for row in rows})==len(rows),'pagination_unverified')
                return rows
            token=pagination.get('nextPaginationToken')
            require(isinstance(token,str) and 0<len(token)<=2048 and token not in seen,'pagination_unverified')
            seen.add(token);start+=len(page)
        raise ConnectSafelyError('history_incomplete')

    def _post_reactions(self,plan):
        token=None;seen=set()
        for _ in range(3):
            body={'accountId':self.account_id,'postUrl':plan['post_url'],'count':10}
            if token is not None:body['paginationToken']=token
            data=self._call('POST','/linkedin/posts/reactions',body=body)
            self._scope(data)
            require(data.get('accountId')==self.account_id and post_identity(data.get('postUrl'))==plan['post_urn'],'post_identity_mismatch')
            details=data.get('postDetails')
            require(isinstance(details,dict) and details.get('activityUrn')==plan['post_urn'],'post_identity_mismatch')
            rows=data.get('reactions');pagination=data.get('pagination')
            require(isinstance(rows,list) and len(rows)<=10 and isinstance(pagination,dict) and type(pagination.get('hasNextPage')) is bool,'pagination_unverified')
            require(type(pagination.get('count')) is int and pagination['count']==len(rows),'pagination_unverified')
            for raw in rows:
                row=select(raw,{'actorType':str,'actorPublicIdentifier':str,'postUrn':str,'reactionType':str})
                require(row.get('postUrn')==plan['post_urn'] and row.get('actorType') in ('profile','company'),'reaction_identity_unverified')
                if row['actorType']=='profile':
                    require(isinstance(row.get('actorPublicIdentifier'),str) and row['actorPublicIdentifier'],'reaction_identity_unverified')
                    require(row['actorPublicIdentifier'].lower()!=self.expected_public_id.lower(),'existing_reaction')
            if pagination['hasNextPage'] is False:
                require('nextPaginationToken' in pagination and pagination['nextPaginationToken'] is None,'pagination_unverified');return
            token=pagination.get('nextPaginationToken')
            require(isinstance(token,str) and 0<len(token)<=2048 and token not in seen,'pagination_unverified');seen.add(token)
        raise ConnectSafelyError('history_incomplete')

    def _social_quota(self,action):
        feature='COMMENT' if action in ('comment','reply') else self.reaction_quota_feature
        require(feature is not None,'quota_contract_unverified')
        rows=[r for r in self.quota()['quotas'] if r['feature']==feature]
        require(rows and len({r['window'] for r in rows})==len(rows),'quota_contract_unverified')
        for row in rows:
            require(row['window'] in ('daily','weekly') and row['used']>=0 and row['limit']>0 and row['remaining']==max(0,row['limit']-row['used']),'quota_contract_unverified')
            try:
                reset=datetime.fromisoformat(row['resetAt'].replace('Z','+00:00'))
                require(reset.tzinfo is not None and reset.timestamp()>self.clock(),'quota_contract_unverified')
            except (ValueError,OverflowError):raise ConnectSafelyError('quota_contract_unverified') from None
            require(row['remaining']>0 and row['limitReached'] is False,'quota_exhausted')

    def send(self,plan,*,before_dispatch):
        # An official transport needs a live, scoped database capability before
        # any preflight reads, and consumes it again at the final wire boundary.
        from .connectsafely_social_transport import ConnectSafelySocialHTTPS
        require(callable(before_dispatch),'dispatch_authority_unavailable');self._plan(plan)
        if isinstance(self.request,ConnectSafelySocialHTTPS):self.request.preflight(plan)
        self.verify_identity(for_write=True)
        kind='reactions' if plan['action']=='react_post' else 'comments'
        history=self.history(kind)
        existing=[row for row in history if self._target(row,kind)==plan['post_urn']]
        require(not existing,'existing_reaction' if kind=='reactions' else 'existing_comment')
        if kind=='reactions':
            self._post_reactions(plan);comments=[]
        else:
            comments=self._post_comments(plan)
            require(not any(same_profile(row.get('commenterUrn'),self.self_profile_id) or same_profile(row.get('commenterProfileId'),self.self_profile_id) or row.get('publicIdentifier','').lower()==self.expected_public_id.lower() for row in comments),'existing_comment')
        if plan['action']=='reply':require(sum(row['commentId']==plan['parent_comment_urn'] for row in comments)==1,'parent_comment_unverified')
        path,body=social_wire_request(plan)
        self._social_quota(plan['action'])
        require(before_dispatch() is True,'dispatch_authority_denied')
        try:
            data=self._call('POST',path,body=body)
            self._scope(data)
            if plan['action']!='reply':
                require(data.get('accountId')==self.account_id and post_identity(data.get('postUrl'))==plan['post_urn'],'receipt_unverified')
                if plan['action']=='comment':require(data.get('comment')==plan['content'],'receipt_unverified')
                else:require(data.get('threadUrn')==plan['post_urn'] and data.get('reactionType')==plan['reaction_type'] and not data.get('companyUrn'),'receipt_unverified')
            return {'status':'uncertain','error_code':'social_receipt_unavailable'}
        except ConnectSafelyError as exc:return {'status':'uncertain','error_code':str(exc)}

    def reconcile(self,plan,attempted_at):
        self._plan(plan)
        require(type(attempted_at) in (int,float) and math.isfinite(attempted_at) and 0<attempted_at<=self.clock(),'invalid_attempt_time')
        if plan['action']=='reply':return {'status':'uncertain','error_code':'reply_parent_receipt_unavailable'}
        kind='reactions' if plan['action']=='react_post' else 'comments'
        rows=self.history(kind);matches=[]
        for row in rows:
            if self._target(row,kind)!=plan['post_urn']:continue
            if not attempted_at<=self._time(row.get('timestamp'))<=min(attempted_at+120,self.clock()+5):continue
            if kind=='comments':
                if row.get('commentText')!=plan['content']:continue
                identifier=row.get('commentUrn');match=COMMENT.fullmatch(identifier or '')
                # Without a proven activity mapping do not assume a share/UGC id.
                if not match or match.group(2)!=plan['post_urn']:continue
                matches.append(identifier)
            elif row.get('reactionType')==plan['reaction_type']:matches.append(True)
        if len(matches)!=1:return {'status':'uncertain','error_code':'receipt_missing_or_ambiguous'}
        if kind=='reactions':return {'status':'uncertain','error_code':'reaction_receipt_unavailable','observed_match':True,'write_attribution_confirmed':False}
        return {'status':'uncertain','error_code':'comment_parent_receipt_unavailable','observed_match':True,'write_attribution_confirmed':False}
