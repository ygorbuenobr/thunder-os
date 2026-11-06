"""Non-dispatching Apollo LinkedIn execution preparation.

This module contains no ConnectSafely transport or Apollo mutation. Reservations
are reserved only by a future authorized execution; dry runs do not consume identities.
"""
from contextlib import nullcontext

from .apollo_linkedin_preview import _slug, validated_task_text
from .apollo_linkedin_attention import EXCLUDED_TASK

_ACTIONS = {'linkedin_step_connect': 'invite', 'linkedin_step_message': 'message'}


class DryRunExecutor:
    def __init__(self, service, apollo, bindings, account_status_reader, *, client=None, execution_enabled=False,
                 invite_limit=300, message_limit=1000):
        if execution_enabled is not False:
            raise ValueError('execution_forbidden')
        if service is None or not service.apollo_linkedin_store_ready():
            raise RuntimeError('official_store_unavailable')
        self.service, self.apollo = service, apollo
        self.bindings, self.account_status_reader, self.client = bindings, account_status_reader, client
        self.invite_limit, self.message_limit = invite_limit, message_limit

    def prepare(self, task, *, company_id, team_id, read_only=False, expected_text_hash=None):
        if not isinstance(task, dict):
            return {'state': 'blocked', 'reason': 'identity_unverified'}
        if task.get('id') == EXCLUDED_TASK:
            return {'state': 'blocked', 'reason': 'fixture_protegida'}
        action = _ACTIONS.get(task.get('type'))
        company, task_id = company_id, task.get('id')
        if read_only and self.client is None:
            _, text_hash, blocker = validated_task_text(
                task, action, invite_limit=self.invite_limit, message_limit=self.message_limit)
            if blocker:
                return {'state': 'blocked', 'reason': blocker, 'execution_enabled': False}
            observation = self.bindings.observe_account(company_id=company_id, team_id=team_id,
                campaign_id=task.get('emailer_campaign_id'), owner_id=task.get('user_id'),
                account_status_reader=self.account_status_reader)
            if observation.get('status') != 'observed':
                return {'state': 'blocked', 'reason': observation.get('reason', 'account_status_unavailable'),
                        'text_hash': text_hash, 'execution_enabled': False}
            return {'state': 'blocked', 'reason': 'execution_binding_unverified',
                    'account_observation': observation, 'text_hash': text_hash, 'execution_enabled': False}
        binding = self.bindings.resolve(company_id=company_id, team_id=team_id,
            campaign_id=task.get('emailer_campaign_id'), owner_id=task.get('user_id'),
            account_status_reader=self.account_status_reader, client=self.client)
        if binding.get('status') != 'ready':
            return {'state': 'blocked', 'reason': binding.get('reason', 'binding_unavailable')}
        account = binding['account_id']
        if not action or not all(isinstance(x, str) and x for x in (company, account, task_id)):
            return {'state': 'blocked', 'reason': 'identity_unverified'}
        if task.get('status') != 'scheduled':
            return {'state': 'blocked', 'reason': 'task_not_open'}
        text, text_hash, blocker = validated_task_text(
            task, action, invite_limit=self.invite_limit, message_limit=self.message_limit)
        if blocker:
            return {'state': 'blocked', 'reason': blocker}
        if not isinstance(expected_text_hash, str) or len(expected_text_hash) != 64 or any(
                c not in '0123456789abcdef' for c in expected_text_hash):
            return {'state': 'blocked', 'reason': 'text_reference_missing'}
        if expected_text_hash != text_hash:
            return {'state': 'blocked', 'reason': 'text_changed'}
        contact = task.get('contact') or {}
        recipient = _slug(contact.get('linkedin_url')) if isinstance(contact, dict) else None
        if not recipient:
            return {'state': 'blocked', 'reason': 'profile_unverified'}
        if read_only and any(lock['account_id'] == account for lock in self.service.list_apollo_linkedin_account_locks()):
            return {'state': 'blocked', 'reason': 'account_locked'}
        lock = nullcontext() if read_only else self.service.apollo_linkedin_account_lock(account)
        with lock:
            try:
                current = self.apollo.get(task_id)
            except ValueError as exc:
                if str(exc) == 'task_removed':
                    return {'state': 'blocked', 'reason': 'task_removed'}
                return {'state': 'blocked', 'reason': 'reread_unavailable'}
            except Exception:
                return {'state': 'blocked', 'reason': 'reread_unavailable'}
            if not isinstance(current, dict):
                return {'state': 'blocked', 'reason': 'reread_unavailable'}
            _, current_hash, current_blocker = validated_task_text(
                current, action, invite_limit=self.invite_limit, message_limit=self.message_limit)
            if current_blocker or current_hash != text_hash:
                return {'state': 'blocked', 'reason': 'text_changed'}
            if current != task or current.get('status') != 'scheduled':
                return {'state': 'blocked', 'reason': 'task_changed'}
            if action == 'invite':
                path, body = '/linkedin/connect', {'accountId': account, 'profileId': recipient, 'profileUrn': None, 'customMessage': text}
            else:
                path, body = '/linkedin/conversations/send', {'accountId': account, 'recipientProfileId': recipient, 'recipientProfileUrn': None, 'messagingChannel': 'linkedin_inbox', 'message': text}
            return {'state': 'dry_run', 'execution_enabled': False, 'text_hash': text_hash,
                    'call_preview': {'method': 'POST', 'path': path, 'body': body, 'executable': False}}
