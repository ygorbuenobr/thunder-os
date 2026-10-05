"""Manual GET-only thread observations, not a poller, webhook or reply sender.

Partial pages can establish individual observations, never absence/completeness.
A missing backwards cursor cannot advance the independent forward checkpoint.
"""
import re
from urllib.parse import quote
from .connectsafely_client import require,urn
from .models import payload_hash
from .db import RepositoryConflict


class ConnectSafelyInbox:
    def __init__(self,repository,client):
        self.repo,self.client=repository,client

    def poll(self,plan,conversation_urn,*,anchor_ms):
        c=self.client;c._plan(plan)
        require(type(anchor_ms) is int and 0<anchor_ms<10**16,'pagination_unverified')
        require(urn(conversation_urn),'invalid_conversation')
        c.verify_identity()
        binding=c._call('GET','/linkedin/conversations/exists/'+quote(plan['recipient_public_id'],safe=''),{'accountId':c.account_id})
        c._scope(binding)
        require(binding.get('accountId')==c.account_id and binding.get('profileUrn')==plan['recipient_profile_urn']
                and binding.get('hasConversation') is True and binding.get('conversationUrn')==conversation_urn,'recipient_identity_mismatch')
        require(plan.get('conversation_urn') is None or plan['conversation_urn']==conversation_urn,'conversation_identity_mismatch')
        scope=payload_hash({'account':c.account_id,'thread':conversation_urn,'recipient':plan['recipient_profile_urn']})
        checkpoint=self.repo.get_state('connectsafely_inbox_checkpoint',scope)
        if checkpoint and checkpoint['anchor_ms']!=anchor_ms:raise RepositoryConflict('inbox_anchor_changed')
        since=checkpoint['since_ms'] if checkpoint else anchor_ms
        page=c.messages(conversation_urn,since=since)
        owner=c._profile_identity(c.self_profile_id);recipient=c._profile_identity(plan['recipient_profile_urn'])
        require(owner is not None and recipient is not None and owner!=recipient,'participants_unverified')
        aliases={c.expected_public_id:owner,plan['recipient_public_id']:recipient}
        identity=lambda value:aliases.get(value,c._profile_identity(value))
        ids=[identity(r.get('profileId')) for r in page['participants']]
        # The provider can repeat one person as publicId, bare ID and profile URN.
        # Collapse only aliases already bound above; any third identity still fails.
        require(2<=len(ids)<=6 and set(ids)=={owner,recipient},'participants_unverified')
        rows=[];seen=set();previous=since
        for row in page['messages']:
            mid=row.get('messageUrn');require(urn(mid),'receipt_unverified')
            modern=bool(re.fullmatch(r'urn:li:msg_message:\(urn:li:fsd_profile:'+re.escape(owner)+r',[A-Za-z0-9_.=-]+\)',mid))
            require(mid.startswith('urn:li:messagingMessage:') or modern,'receipt_unverified')
            stamp=int(c._seconds(row.get('sentAt'))*1000)
            require(since<=stamp<=int(c.clock()*1000)+5000 and stamp>=previous,'pagination_unverified');previous=stamp
            require(mid not in seen,'pagination_unverified');seen.add(mid)
            sender=identity(row.get('senderProfileId'));outgoing=row.get('isSentByOwner')
            require(type(outgoing) is bool and sender==(owner if outgoing else recipient),'participants_unverified')
            rows.append({'account_id':c.account_id,'conversation_urn':conversation_urn,'message_urn':mid,
                         'sender_profile_id':sender,'recipient_profile_id':recipient if outgoing else owner,
                         'sent_at_ms':stamp,'outgoing':outgoing,'content':row.get('text'),
                         'source':'observed_provider','body_complete':isinstance(row.get('text'),str)})
        complete=page['has_more'] is False and page['fetching_more'] is False
        imported=[];duplicates=0;conflicts=[]
        with self.repo.transaction():
            current=self.repo.get_state('connectsafely_inbox_checkpoint',scope)
            if current!=checkpoint:raise RepositoryConflict('inbox_checkpoint_changed')
            for message in rows:
                key=payload_hash({'account':c.account_id,'thread':conversation_urn,'message':message['message_urn']})
                fingerprint=payload_hash(message);old=self.repo.get_state('connectsafely_inbox_message',key)
                if old:
                    if old['source_hash']==fingerprint:duplicates+=1;continue
                    revision=key+':'+fingerprint
                    if not self.repo.get_state('connectsafely_inbox_revision',revision):
                        self.repo.append_state('connectsafely_inbox_revision',revision,{'message':message,'source_hash':fingerprint,'original_id':key,'state':'review_required'})
                    conflicts.append(key);continue
                self.repo.append_state('connectsafely_inbox_message',key,{'message':message,'source_hash':fingerprint,'scope':scope})
                if not message['outgoing']:imported.append(message)
            next_since=max(since,max((r['sent_at_ms'] for r in rows),default=since)-1) if complete and not conflicts else since
            self.repo.put_state('connectsafely_inbox_checkpoint',scope,{'anchor_ms':anchor_ms,'since_ms':next_since,
                'window_complete':complete and not conflicts,'last_observed_at':c.clock(),'observed_count':len(rows),
                'coverage':'complete_response_window' if complete and not conflicts else 'partial_response_window'})
        return {'scope':scope,'source':'observed_provider','incoming':imported,'observed_incoming':[r for r in rows if not r['outgoing']],
                'observed_count':len(rows),'duplicates':duplicates,'conflicts':conflicts,'since_requested_ms':since,
                'next_since_ms':next_since,'window_complete':complete and not conflicts,'provider_writes':0}
