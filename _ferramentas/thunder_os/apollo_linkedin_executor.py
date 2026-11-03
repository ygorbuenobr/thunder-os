"""Non-dispatching Apollo LinkedIn execution preparation.

This module contains no ConnectSafely transport or Apollo mutation. Reservations
are reserved only by a future authorized execution; dry runs do not consume identities.
"""
from .apollo_linkedin_preview import _slug

_ACTIONS = {'linkedin_step_connect': 'invite', 'linkedin_step_message': 'message'}


class DryRunExecutor:
    def __init__(self, service, apollo, bindings, account_status_reader, *, execution_enabled=False):
        if execution_enabled is not False:
            raise ValueError('execution_forbidden')
        if service is None or not service.apollo_linkedin_store_ready():
            raise RuntimeError('official_store_unavailable')
        self.service, self.apollo = service, apollo
        self.bindings, self.account_status_reader = bindings, account_status_reader

    def prepare(self, task, *, company_id, team_id):
        if not isinstance(task, dict):
            return {'state': 'blocked', 'reason': 'identity_unverified'}
        action = _ACTIONS.get(task.get('type'))
        company, task_id = company_id, task.get('id')
        binding = self.bindings.resolve(company_id=company_id, team_id=team_id,
            campaign_id=task.get('emailer_campaign_id'), owner_id=task.get('user_id'),
            account_status_reader=self.account_status_reader)
        if binding.get('status') != 'ready':
            return {'state': 'blocked', 'reason': binding.get('reason', 'binding_unavailable')}
        account = binding['account_id']
        if not action or not all(isinstance(x, str) and x for x in (company, account, task_id)):
            return {'state': 'blocked', 'reason': 'identity_unverified'}
        if task.get('status') != 'scheduled':
            return {'state': 'blocked', 'reason': 'task_not_open'}
        contact = task.get('contact') or {}
        recipient = _slug(contact.get('linkedin_url')) if isinstance(contact, dict) else None
        if not recipient:
            return {'state': 'blocked', 'reason': 'profile_unverified'}
        with self.service.apollo_linkedin_account_lock(account):
            try:
                current = self.apollo.get(task_id)
            except Exception:
                return {'state': 'blocked', 'reason': 'reread_unavailable'}
            if current != task or current.get('status') != 'scheduled':
                return {'state': 'blocked', 'reason': 'task_changed'}
            if action == 'invite':
                path, body = '/linkedin/connect', {'accountId': account, 'profileId': recipient, 'profileUrn': None, 'customMessage': None}
            else:
                path, body = '/linkedin/conversations/send', {'accountId': account, 'recipientProfileId': recipient, 'recipientProfileUrn': None, 'messagingChannel': 'linkedin_inbox', 'message': None}
            return {'state': 'dry_run', 'execution_enabled': False, 'call_preview': {'method': 'POST', 'path': path, 'body': body, 'executable': False}}
