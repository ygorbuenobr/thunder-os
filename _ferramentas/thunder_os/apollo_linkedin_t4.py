"""T4 coordinator simulation. It must not be wired to a live runtime.

All ports must be explicitly supplied by a trusted composition. In particular,
`campaigns.approved` and `canary.consume` are security-critical contracts, not
boolean configuration flags; the real composition is deliberately not provided.
`simulation=True` is not a security boundary: only use fake ports in tests.
The default remains dry-run. A reserved row left by a crash is never replayed.
"""
from __future__ import annotations

import os
import math
import argparse
import json
import re
import time
import threading
from contextlib import contextmanager

from .apollo_linkedin_attention import EXCLUDED_TASK
from .apollo_linkedin_preview import _slug, validated_task_text
from .campaign_registry import sequence_allowed


class BeforeDispatchError(Exception):
    """The adapter proves that no provider write was attempted."""


class FakeT4Store:
    """Ephemeral store; has no filesystem, database, or network access."""
    __slots__ = ('rows', 'lock', 'fail_confirmation')
    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()
        self.fail_confirmation = False

    def apollo_linkedin_store_ready(self): return True

    @contextmanager
    def apollo_linkedin_account_lock(self, account):
        if not self.lock.acquire(False): raise ValueError('locked')
        try: yield
        finally: self.lock.release()

    def reserve_apollo_linkedin(self, company, task_id, action_type, account_id, **kw):
        key = (company, task_id, action_type)
        if key in self.rows: raise ValueError('duplicate')
        self.rows[key] = {'company': company, 'task_id': task_id, 'action_type': action_type,
                          'state': 'reserved', **kw}

    def finish_apollo_linkedin(self, *args, **kw):
        company, task_id, action_type, state, reason = args
        if state == 'confirmed' and self.fail_confirmation:
            raise ValueError('synthetic commit failure')
        row = self.rows[(company, task_id, action_type)]
        if row['state'] != 'reserved': raise ValueError('invalid transition')
        row.update(state=state, reason=reason, receipt=kw.get('receipt'), apollo_status=kw.get('apollo_status'))

    def list_apollo_linkedin_reservations(self): return list(self.rows.values())

    def claim_apollo_linkedin_completion(self, *key):
        if self.rows[key].get('apollo_status') != 'pending': raise ValueError('claim unavailable')
        self.rows[key]['apollo_status'] = 'attempted'

    def reconcile_apollo_linkedin(self, *key):
        if self.rows[key].get('apollo_status') != 'attempted': raise ValueError('claim unavailable')
        self.rows[key]['apollo_status'] = 'confirmed'


class FakeT4Apollo:
    """Synthetic task and completion, with no transport or external state."""
    __slots__ = ('completed', 'fail_complete', 'removed', 'text', 'complete_calls')
    def __init__(self):
        self.completed = False
        self.fail_complete = False
        self.removed = False
        self.text = 'Convite de teste'
        self.complete_calls = 0

    def get(self, task_id):
        if self.removed: raise ValueError('task_removed')
        return {'id': task_id, 'status': 'completed' if self.completed else 'scheduled',
                'type': 'linkedin_step_connect', 'emailer_campaign_id': 'seq', 'user_id': 'owner-id',
                'contact': {'linkedin_url': 'https://www.linkedin.com/in/lead/'},
                'linkedin_emailer_template': {'body_text': self.text}}

    def complete(self, task_id):
        self.complete_calls += 1
        if self.fail_complete: raise RuntimeError('apollo unavailable')
        self.completed = True


class FakeT4LinkedIn:
    """Synthetic action port: data-only responses; never opens a socket."""
    __slots__ = ('calls', 'response', 'error', 'pending', 'after')

    def __init__(self):
        self.calls = 0
        self.response = {'status': 'confirmed', 'provider_id': 'synthetic-receipt'}
        self.error = None
        self.pending = False
        self.after = False

    def account_status(self):
        return {'id': 'account', 'publicId': 'owner', 'available': True}

    def invitation_observation(self, recipient):
        return {'complete': True, 'present': self.pending or self.after}

    def send_invitation(self, account, recipient, text):
        self.calls += 1
        if self.error:
            raise self.error
        return dict(self.response)


class FakeT4Campaigns:
    __slots__ = ('binding',)
    def __init__(self, binding): self.binding = dict(binding)
    def approved(self, campaign_id, version): return dict(self.binding)


class FakeT4Safety:
    __slots__ = ('data', 'pause_calls', 'pause_persisted')
    def __init__(self):
        self.pause_calls = 0
        self.pause_persisted = True
        self.data = {'coverage': 'complete', 'human_reply': False, 'suppressed': False,
                     'lead_touches_today': 0, 'weekly_sent_all_channels': 0, 'provider_remaining': 1,
                     'last_account_action_at': None, 'account_history_complete': True}
    def global_paused(self): return False
    def inspect(self, *args): return dict(self.data)
    def pause_after_reply(self, company, recipient):
        self.pause_calls += 1
        return {'persisted': self.pause_persisted}


class FakeT4Canary:
    __slots__ = ('used',)
    def __init__(self): self.used = False
    def consume(self, *args):
        if self.used: return False
        self.used = True
        return True


class T4SimulationCoordinator:
    def __init__(self, *, repo, campaigns, apollo, linkedin, safety, canary, clock=time.time, simulation=False):
        if (simulation is not True or type(linkedin) is not FakeT4LinkedIn
                or type(apollo) is not FakeT4Apollo or type(repo) is not FakeT4Store
                or type(campaigns) is not FakeT4Campaigns or type(safety) is not FakeT4Safety
                or type(canary) is not FakeT4Canary):
            raise ValueError('simulation_only')
        self.repo, self.campaigns, self.apollo = repo, campaigns, apollo
        self.linkedin, self.safety, self.canary, self.clock = linkedin, safety, canary, clock
        if repo is None or not repo.apollo_linkedin_store_ready():
            raise RuntimeError('official_store_unavailable')

    def _gate(self):
        return (os.environ.get('THUNDER_APOLLO_LI_EXECUTION_ENABLED') == 'true'
                and self.safety.global_paused() is False)

    @staticmethod
    def _valid_binding(binding, campaign_id, version):
        return (isinstance(binding, dict) and binding.get('approved') is True
                and binding.get('campaign_id') == campaign_id
                and str(binding.get('version')) == str(version)
                and all(isinstance(binding.get(key), str) and binding[key].strip()
                        and binding[key] == binding[key].strip()
                        for key in ('company_id', 'account_id', 'account_public_id'))
                and (sequence_allowed(binding, binding.get('sequence_id'))
                     or (isinstance(binding.get('sequence_ids'), list) and bool(binding['sequence_ids'])
                         and sequence_allowed(binding, binding['sequence_ids'][0])))
                and isinstance(binding.get('owner_ids'), list) and bool(binding['owner_ids'])
                and all(isinstance(owner, str) and owner for owner in binding['owner_ids'])
                and type(binding.get('weekly_limit')) is int and binding['weekly_limit'] > 0
                and type(binding.get('spacing_seconds')) is int and 40 <= binding['spacing_seconds'] <= 90)

    @staticmethod
    def _task_matches(task, task_id, binding, preview):
        if not isinstance(task, dict) or task.get('id') != task_id:
            return 'task_unavailable'
        if task.get('status') != 'scheduled':
            return 'task_not_open'
        if task.get('type') != 'linkedin_step_connect':
            return 'unsupported_action'
        if (not sequence_allowed(binding, task.get('emailer_campaign_id'))
                or task.get('user_id') not in binding['owner_ids']):
            return 'task_binding_changed'
        contact = task.get('contact')
        recipient = _slug(contact.get('linkedin_url')) if isinstance(contact, dict) else None
        if not recipient or recipient != preview['recipient']:
            return 'recipient_changed'
        if recipient == binding['account_public_id'].lower():
            return 'self_invitation'
        _, digest, blocker = validated_task_text(task, 'invite')
        if blocker or digest != preview['text_hash']:
            return 'text_changed'
        return None

    def _safety_reason(self, binding, recipient):
        data = self.safety.inspect(binding['company_id'], binding['account_id'], recipient)
        if not isinstance(data, dict) or data.get('coverage') != 'complete':
            return 'coverage_unavailable'
        if data.get('human_reply') is not False:
            if data.get('human_reply') is not True:
                return 'coverage_unavailable'
            try:
                pause = self.safety.pause_after_reply(binding['company_id'], recipient)
                if not isinstance(pause, dict) or pause.get('persisted') is not True:
                    return 'human_reply_pause_unverified'
            except Exception:
                return 'human_reply_pause_unverified'
            return 'human_reply'
        if data.get('suppressed') is not False:
            return 'suppressed' if data.get('suppressed') is True else 'coverage_unavailable'
        count, sent, remaining = (data.get('lead_touches_today'), data.get('weekly_sent_all_channels'),
                                  data.get('provider_remaining'))
        if any(type(value) is not int or value < 0 for value in (count, sent, remaining)):
            return 'coverage_unavailable'
        if count:
            return 'lead_daily_limit'
        if sent >= binding['weekly_limit'] or remaining == 0:
            return 'quota_exhausted'
        if data.get('account_history_complete') is not True:
            return 'pacing_unverified'
        last = data.get('last_account_action_at')
        if last is not None:
            now = self.clock()
            if (type(last) not in (int, float) or not math.isfinite(last)
                    or type(now) not in (int, float) or not math.isfinite(now)
                    or last < 0 or last > now):
                return 'pacing_unverified'
            if now - last < binding['spacing_seconds']:
                return 'pacing_wait'
        return None

    def run(self, task_id, campaign_id, preview, *, dry_run=True):
        if task_id == EXCLUDED_TASK:
            return {'state': 'blocked', 'reason': 'fixture_protegida'}
        if (not isinstance(task_id, str) or not task_id or not isinstance(campaign_id, str)
                or not campaign_id or not isinstance(preview, dict)):
            return {'state': 'blocked', 'reason': 'identity_unverified'}
        if (not isinstance(preview.get('text_hash'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', preview['text_hash'])
                or not isinstance(preview.get('recipient'), str)
                or not preview['recipient'] or preview.get('version') is None):
            return {'state': 'blocked', 'reason': 'preview_unverified'}
        if dry_run is not False:
            return {'state': 'dry_run', 'execution_enabled': False}
        try:
            if not self._gate():
                return {'state': 'blocked', 'reason': 'execution_disabled'}
            binding = self.campaigns.approved(campaign_id, preview['version'])
            if not self._valid_binding(binding, campaign_id, preview['version']):
                return {'state': 'blocked', 'reason': 'campaign_unapproved'}
            with self.repo.apollo_linkedin_account_lock(binding['account_id']):
                return self._run_locked(task_id, binding, preview)
        except Exception:
            # Includes contention and provider dependency failure. Never retry.
            return {'state': 'blocked', 'reason': 'preflight_unavailable'}

    def _run_locked(self, task_id, binding, preview):
        key = (binding['company_id'], task_id, 'linkedin_step_connect')
        self.repo.reserve_apollo_linkedin(*key, binding['account_id'], text_hash=preview['text_hash'],
                                          campaign_version=str(binding['version']), recipient=preview['recipient'])
        def finish(state, reason=None, **kwargs):
            self.repo.finish_apollo_linkedin(*key, state, reason, **kwargs)
            return {'state': state, 'reason': reason}
        try:
            task = self.apollo.get(task_id)
            reason = self._task_matches(task, task_id, binding, preview)
            if reason:
                return finish('rejected', reason)
            account = self.linkedin.account_status()
            if (not isinstance(account, dict) or account.get('id') != binding['account_id']
                    or account.get('publicId') != binding['account_public_id'] or account.get('available') is not True):
                return finish('rejected', 'account_unavailable')
            reason = self._safety_reason(binding, preview['recipient'])
            if reason:
                return finish('rejected', reason)
            prior = self.linkedin.invitation_observation(preview['recipient'])
            if not isinstance(prior, dict) or prior.get('complete') is not True or type(prior.get('present')) is not bool:
                return finish('rejected', 'invitation_history_unavailable')
            if prior['present']:
                return finish('uncertain', 'prior_invitation')
            if not self._gate():
                return finish('rejected', 'execution_disabled')
            # The port must atomically consume the one-shot authorization on the
            # official store. A mere caller-supplied boolean is not such proof.
            if self.canary.consume(task_id, binding['company_id'], binding['account_id']) is not True:
                return finish('rejected', 'canary_unavailable')
            reason = self._safety_reason(binding, preview['recipient'])
            if reason or not self._gate():
                return finish('rejected', reason or 'execution_disabled')
        except Exception as exc:
            return finish('rejected', 'task_removed' if str(exc) == 'task_removed' else 'preflight_unavailable')
        try:
            # An exception here may have occurred after bytes were sent.
            result = self.linkedin.send_invitation(binding['account_id'], preview['recipient'],
                                                   task['linkedin_emailer_template']['body_text'])
        except BeforeDispatchError:
            return finish('rejected', 'rejected_before_dispatch')
        except Exception:
            return finish('uncertain', 'send_outcome_unknown')
        if not isinstance(result, dict) or result.get('status') not in ('confirmed', 'accepted'):
            return finish('uncertain', 'send_outcome_unknown')
        provider_id = result.get('provider_id')
        if isinstance(provider_id, str) and provider_id.strip():
            receipt = {'provider_id': provider_id}
        else:
            try:
                observed = self.linkedin.invitation_observation(preview['recipient'])
            except Exception:
                observed = None
            if not isinstance(observed, dict) or observed.get('complete') is not True or observed.get('present') is not True:
                return finish('uncertain', 'receipt_missing')
            receipt = {'publicIdentifier': preview['recipient']}
        try:
            finish('confirmed', None, receipt=receipt, apollo_status='pending')
        except Exception:
            # The provider may have acted even if the local commit is unknown.
            return {'state': 'uncertain', 'reason': 'receipt_persistence_unverified'}
        return self._reconcile(key, allow_first_completion=True)

    def _reconcile(self, key, *, allow_first_completion=False):
        try:
            result = self.apollo.get(key[1])
            if not isinstance(result, dict) or result.get('id') != key[1]:
                raise ValueError('completion_unverified')
            if result.get('status') == 'scheduled':
                if not allow_first_completion:
                    raise ValueError('completion_unverified')
                self.repo.claim_apollo_linkedin_completion(*key)
                self.apollo.complete(key[1])
                result = self.apollo.get(key[1])
            if not isinstance(result, dict) or result.get('id') != key[1] or result.get('status') != 'completed':
                raise ValueError('completion_unverified')
            self.repo.reconcile_apollo_linkedin(*key)
            return {'state': 'confirmed', 'reason': None}
        except Exception:
            return {'state': 'apollo_reconcile_pending', 'reason': 'apollo_completion_unverified'}

    def reconcile(self, company, task_id, action_type):
        """Reconcile an existing confirmed receipt; never call LinkedIn."""
        if task_id == EXCLUDED_TASK:
            return {'state': 'blocked', 'reason': 'fixture_protegida'}
        try:
            rows = self.repo.list_apollo_linkedin_reservations()
            matches = [row for row in rows if (row['company'], row['task_id'], row['action_type']) ==
                       (company, task_id, action_type)]
            if len(matches) != 1 or matches[0].get('state') != 'confirmed' or matches[0].get('apollo_status') != 'attempted' or not matches[0].get('receipt'):
                return {'state': 'blocked', 'reason': 'confirmed_receipt_required'}
            return self._reconcile((company, task_id, action_type))
        except Exception:
            return {'state': 'blocked', 'reason': 'reconciliation_unavailable'}


def main(argv=None):
    """CLI is intentionally non-operational until trusted composition exists."""
    parser = argparse.ArgumentParser(description='Apollo LinkedIn T4 — default dry-run')
    parser.add_argument('--task', required=True)
    parser.add_argument('--authorization')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    if args.task == EXCLUDED_TASK:
        result = {'state': 'blocked', 'reason': 'fixture_protegida', 'executable': False}
    elif args.execute:
        result = {'state': 'blocked', 'reason': 'live_composition_unavailable', 'executable': False}
    else:
        result = {'state': 'dry_run', 'reason': 'live_composition_unavailable', 'executable': False}
    print(json.dumps(result))
    return 2 if result['state'] == 'blocked' else 0


if __name__ == '__main__':
    raise SystemExit(main())
