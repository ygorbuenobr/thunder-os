"""Exact approved evidence lookup; no URL-to-URN heuristics or provider calls."""
import re
from urllib.parse import urlsplit


def resolve_target(body,*,account_id,public_id,load_evidence):
    def blocked(code):
        return {'resolved':False,'read_only':True,'actions_available':False,'status':'blocked','error_code':code}
    if not isinstance(body,dict) or set(body)-{'action','post_url','parent_comment_url'} or body.get('action') not in {'comment','reply','react_post'}:
        return blocked('invalid_target_request')
    url=body.get('post_url')
    if not isinstance(url,str) or not re.fullmatch(r'https://www\.linkedin\.com/feed/update/urn:li:activity:[0-9]{1,30}/',url):
        return blocked('post_target_unverified')
    parent=body.get('parent_comment_url')
    if body['action']=='reply':
        # Current approved author contract has no comment permalink field.
        # Do not choose a parent merely because only one binding exists.
        return blocked('comment_target_unverified')
    if parent is not None:return blocked('invalid_target_request')
    try:
        source=load_evidence()
        if source.get('account_id')!=account_id:return blocked('target_account_unverified')
        rows=source.get('bindings')
        if not isinstance(rows,list) or len(rows)>1000:return blocked('post_target_unverified')
        matches=[r for r in rows if isinstance(r,dict) and r.get('post_url')==url and r.get('parent_comment_urn') is None]
        if len(matches)!=1:return blocked('post_target_unverified')
        row=matches[0]
        if set(row)!={'post_url','post_urn','public_id','profile_urn'}:return blocked('post_target_unverified')
        if url!='https://www.linkedin.com/feed/update/'+row['post_urn']+'/':return blocked('post_target_unverified')
        if not isinstance(row['public_id'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,150}',row['public_id']):return blocked('post_target_unverified')
        if not isinstance(row['profile_urn'],str) or not re.fullmatch(r'urn:li:fsd_profile:[A-Za-z0-9_-]{1,200}',row['profile_urn']):return blocked('post_target_unverified')
        return {'resolved':True,'source':'approved_author_evidence','read_only':True,'actions_available':False,
                'target':{'account_id':account_id,'account_public_id':public_id,'channel':'linkedin','action':body['action'],
                          'post_urn':row['post_urn'],'post_url':url,'parent_comment_urn':None},
                'preview':{'label':'Publicação de '+row['public_id'],'author_public_id':row['public_id'],'parent_author_public_id':None,'content_available':False}}
    except Exception:return blocked('target_evidence_unverified')
