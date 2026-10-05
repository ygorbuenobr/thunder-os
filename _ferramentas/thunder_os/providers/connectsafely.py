"""One LinkedIn façade; all protocol reads/writes delegate to reviewed clients.

Deliberately not compatible with legacy send({target,content,effect_id}): new
writes need the complete approved account/channel/recipient plan and callback.
There is no transport, credential lookup, retry or fallback implementation here.
"""
from ..connectsafely_client import ConnectSafelyClient,ConnectSafelyError,require
from ..connectsafely_social_client import ConnectSafelySocialClient


class ConnectSafelyAdapter:
    def __init__(self,client,*,social_client=None):
        require(isinstance(client,ConnectSafelyClient),'client_unconfigured')
        require(social_client is None or isinstance(social_client,ConnectSafelySocialClient) and social_client.account_id==client.account_id and social_client.expected_public_id==client.expected_public_id and social_client.self_profile_id==client.self_profile_id,'account_identity_mismatch')
        self.client,self.direct_client,self.social_client=client,client,social_client
        self.account_id=client.account_id;self.expected_public_id=client.expected_public_id

    def with_social_capability(self,capability):
        require(self.social_client is not None,'social_client_unconfigured')
        return type(self)(self.client,social_client=self.social_client.with_capability(capability))

    def _port(self,plan):
        require(isinstance(plan,dict),'invalid_plan')
        if plan.get('action') in ('comment','reply','react_post'):
            require(self.social_client is not None,'social_client_unconfigured');return self.social_client
        return self.client

    def _plan(self,plan):return self._port(plan)._plan(plan)
    def verify_identity(self):return self.client.verify_identity()
    def accounts(self):return self.client.accounts()
    def status(self):return self.client.account_status()
    def account_available(self):return self.status()['available']
    def inbox(self,**options):return self.client.inbox(**options)
    def conversations(self,limit=10):return self.client.conversations(limit)
    def messages(self,conversation_urn,limit=10):return self.client.messages(conversation_urn,limit)
    def connections(self,**options):return self.client.connections(**options)
    def sent_invitations(self,**options):return self.client.sent_invitations(**options)
    def quota(self):return self.client.quota()
    def send(self,plan,*,before_dispatch):return self._port(plan).send(plan,before_dispatch=before_dispatch)
    def reconcile(self,plan,attempted_at):return self._port(plan).reconcile(plan,attempted_at)
