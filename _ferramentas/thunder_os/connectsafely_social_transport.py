"""Default-blocked social HTTPS; writes require a scoped database capability.

A reviewed inventory, installed coordinator and reserved effect are verified before
reads and again immediately before the one allowed wire attempt. No env/boolean
activation exists. Capabilities are attached to a per-operation clone, never a
shared mutable request attribute. SQLite and PostgreSQL coordinators must verify
their installed schema, roles, inventory and reservation before every dispatch.
"""
from copy import copy
from .connectsafely_transport import ConnectSafelyHTTPS
from .connectsafely_client import require, ConnectSafelyError
from .connectsafely_social_client import post_identity, social_request_hash, wire_request_hash, COMMENT, REACTIONS


class ConnectSafelySocialHTTPS(ConnectSafelyHTTPS):
    def with_capability(self,capability):
        from .connectsafely_coordination import SocialCapability,SQLiteSocialCoordinator
        from .connectsafely_coordination_postgres import PostgresSocialCoordinator
        from .connectsafely_quarantine_resolution import SQLiteResolvedSocialCoordinator
        from .connectsafely_quarantine_resolution_postgres import PostgresResolvedSocialCoordinator
        require(type(capability) is SocialCapability and type(capability._coordinator) in (SQLiteSocialCoordinator,PostgresSocialCoordinator,SQLiteResolvedSocialCoordinator,PostgresResolvedSocialCoordinator),'social_coordination_unverified')
        cloned=copy(self);cloned._capability=capability
        return cloned

    def _proof(self):
        from .connectsafely_coordination import SocialCapability,SQLiteSocialCoordinator
        from .connectsafely_coordination_postgres import PostgresSocialCoordinator
        from .connectsafely_quarantine_resolution import SQLiteResolvedSocialCoordinator
        from .connectsafely_quarantine_resolution_postgres import PostgresResolvedSocialCoordinator
        cap=getattr(self,'_capability',None)
        require(type(cap) is SocialCapability and type(cap._coordinator) in (SQLiteSocialCoordinator,PostgresSocialCoordinator,SQLiteResolvedSocialCoordinator,PostgresResolvedSocialCoordinator),'social_coordination_unverified')
        require(cap.account_id==self.account_id,'social_coordination_unverified')
        return cap

    def preflight(self,plan):
        cap=self._proof()
        require((cap.action,cap.account_id,cap.post_urn,cap.request_hash)==(plan['action'],plan['account_id'],plan['post_urn'],social_request_hash(plan)),'social_coordination_unverified')
        from .connectsafely_coordination import CoordinationError
        try:require(cap.validate(action=cap.action,account_id=cap.account_id,post_urn=cap.post_urn,request_hash=cap.request_hash) is True,'social_coordination_unverified')
        except CoordinationError:raise ConnectSafelyError('social_coordination_unverified') from None
        return True

    def readiness(self=None):
        blocked={'status':'blocked','writes_enabled':False,'error_code':'social_coordination_unverified','required_action':'Verify every legacy writer, install the shared coordinator, and issue a scoped capability for an already reserved approved effect.'}
        if self is None:return blocked
        try:
            cap=self._proof()
            if cap.validate(action=cap.action,account_id=cap.account_id,post_urn=cap.post_urn,request_hash=cap.request_hash) is not True:return blocked
        except ValueError:return blocked
        return {'status':'ready','writes_enabled':True,'scope':'one_attempt','account_id':self.account_id,'action':cap.action,'post_urn':cap.post_urn}

    def _route(self,method,path,query,body):
        # This is reached before credential loading, TLS context or connection creation.
        # Also denies inherited DM/connect effects; there is no alternate write route.
        write_actions={'/linkedin/posts/comment':'comment','/linkedin/reply':'reply','/linkedin/posts/react':'react_post'}
        if method=='POST' and path in write_actions:
            cap=self._proof()
            allowed={
                'comment':{'accountId','postUrl','comment','tagPostAuthor'},
                'reply':{'accountId','commentId','reply'},
                'react_post':{'accountId','postUrl','threadUrn','reactionType'},
            }[write_actions[path]]
            require(query=={} and isinstance(body,dict) and set(body)==allowed and body.get('accountId')==self.account_id,'invalid_route')
            if path!='/linkedin/reply':require(post_identity(body.get('postUrl'))==cap.post_urn,'invalid_route')
            if path=='/linkedin/posts/react':
                require(body.get('threadUrn')==cap.post_urn and isinstance(body.get('reactionType'),str) and body['reactionType'] in REACTIONS,'invalid_route')
            else:
                content=body.get('comment' if path=='/linkedin/posts/comment' else 'reply')
                require(isinstance(content,str) and 0<len(content.strip())<=1250 and len(content)<=1250 and '@' not in content and '{{' not in content and '}}' not in content and '\x00' not in content,'invalid_route')
                if path=='/linkedin/posts/comment':require(body.get('tagPostAuthor') is False,'invalid_route')
                else:require(isinstance(body.get('commentId'),str) and COMMENT.fullmatch(body['commentId']),'invalid_route')
            # The exact approved parent, content, URL, reaction and channel are hashed.
            request_hash=wire_request_hash(path,body)
            require((cap.action,cap.request_hash)==(write_actions[path],request_hash),'social_coordination_unverified')
            from .connectsafely_coordination import CoordinationError
            try:require(cap.require(action=write_actions[path],account_id=self.account_id,post_urn=cap.post_urn,request_hash=request_hash) is True,'social_coordination_unverified')
            except CoordinationError:raise ConnectSafelyError('social_coordination_unverified') from None
            return
        require(method=='GET' or method=='POST' and path in ('/linkedin/posts/comments','/linkedin/posts/reactions'),'social_coordination_unverified')
        if method=='GET' and path in ('/linkedin/account/'+self.account_id+'/comments','/linkedin/account/'+self.account_id+'/reactions'):
            require(body is None and isinstance(query,dict) and not set(query)-{'count','paginationToken'} and type(query.get('count')) is int and query['count']==10,'invalid_route')
            if 'paginationToken' in query:require(isinstance(query['paginationToken'],str) and 0<len(query['paginationToken'])<=2048,'invalid_route')
            return
        routes={
            '/linkedin/posts/reactions':{'accountId','postUrl','count','paginationToken'},
            '/linkedin/posts/comments':{'accountId','postUrl','commentCount','start','paginationToken'},
        }
        if path not in routes:return super()._route(method,path,query,body)
        require(method=='POST' and query=={} and isinstance(body,dict) and not set(body)-routes[path] and body.get('accountId')==self.account_id,'invalid_route')
        post_identity(body.get('postUrl'))
        if path=='/linkedin/posts/comments':
            require(type(body.get('commentCount')) is int and body['commentCount']==10 and type(body.get('start')) is int and 0<=body['start']<=20,'invalid_route')
            if 'paginationToken' in body:require(isinstance(body['paginationToken'],str) and 0<len(body['paginationToken'])<=2048,'invalid_route')
        elif path=='/linkedin/posts/reactions':
            require(type(body.get('count')) is int and body['count']==10,'invalid_route')
            if 'paginationToken' in body:require(isinstance(body['paginationToken'],str) and 0<len(body['paginationToken'])<=2048,'invalid_route')
