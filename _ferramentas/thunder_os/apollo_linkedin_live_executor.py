"""Fail-closed entry point for the Apollo/LinkedIn canary.

There is intentionally no provider transport here. The legacy send paths do not
share the account lock and pause changes are not fenced with dispatch. Until
those invariants and a legitimate Apollo approval issuer exist, no argument or
environment variable enables a live send or Apollo task completion.
"""
from __future__ import annotations

import argparse
import json
import re
import os
import time
import subprocess
from datetime import datetime, timezone

from .apollo_linkedin_attention import EXCLUDED_TASK
from .apollo_linkedin_preview import _slug, validated_task_text
from .apollo_linkedin_reply_coverage import coverage_current

_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


def hermes_unit_inactive():
    """Inspect the fixed local systemd unit; unknown state always blocks.

    This observation alone does not establish that other native writers are
    absent and therefore cannot enable the live composition by itself.
    """
    try:
        result = subprocess.run(['/usr/bin/systemctl', 'show', 'thunder-os-hermes-linkedin.service',
            '--property=LoadState', '--property=ActiveState', '--property=SubState'],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3, check=False,
            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        if result.returncode != 0:
            return False
        rows = result.stdout.splitlines()
        if len(rows) != 3 or any(row.count('=') != 1 for row in rows):
            return False
        states = dict(row.split('=', 1) for row in rows)
        return states == {'LoadState': 'loaded', 'ActiveState': 'inactive', 'SubState': 'dead'}
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def valid_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def execution_status(task, authorization):
    """Report why execution is impossible without contacting any provider."""
    if not valid_id(task) or not valid_id(authorization):
        return {'status': 'blocked', 'reason': 'configuration_invalid', 'executable': False}
    if task == EXCLUDED_TASK:
        return {'status': 'blocked', 'reason': 'fixture_protegida', 'executable': False}
    return {'status': 'blocked', 'reason': 'live_composition_unavailable',
            'executable': False,
            'missing': ['shared_send_lock', 'pause_dispatch_fence',
                        'apollo_approval_issuer', 'receipt_and_completion_composition']}


class MessageCoordinator:
    """Explicit ports for offline integration tests; not a live runtime factory.

    A production composition must authenticate every port, scope the provider
    credential to the approved account, and prove all other writers fenced.
    This class alone is not evidence that those conditions hold.
    """
    def __init__(self, *, repository, authority, apollo, message, coverage,
                 safety, completion, clock=time.time):
        self.repo, self.authority, self.apollo = repository, authority, apollo
        self.message, self.coverage, self.safety = message, coverage, safety
        self.completion, self.clock = completion, clock

    def _gate(self):
        control = self.repo.get_state('control', 'global')
        return (os.environ.get('THUNDER_APOLLO_LI_EXECUTION_ENABLED') == 'true'
                and isinstance(control, dict) and control.get('paused') is False)

    @staticmethod
    def _task(task, task_id, auth, binding):
        if (not isinstance(task, dict) or task.get('id') != task_id
                or task.get('type') != 'linkedin_step_message' or task.get('status') != 'scheduled'
                or task.get('emailer_campaign_id') != binding['sequence_id']
                or task.get('user_id') not in binding['owner_ids']):
            raise ValueError('task_binding_changed')
        contact = task.get('contact')
        if (not isinstance(contact, dict) or not isinstance(contact.get('id'), str)
                or task.get('contact_id') != contact['id']
                or _slug(contact.get('linkedin_url')) != auth['recipient']):
            raise ValueError('recipient_changed')
        text, digest, error = validated_task_text(task, 'message')
        if error or digest != auth['text_hash']:
            raise ValueError('text_changed')
        return text

    def run(self, task_id, authorization_id, *, dry_run=True):
        if task_id == EXCLUDED_TASK:
            return {'state': 'blocked', 'reason': 'fixture_protegida'}
        if not valid_id(task_id) or not valid_id(authorization_id):
            return {'state': 'blocked', 'reason': 'configuration_invalid'}
        if dry_run is not False:
            return {'state': 'dry_run', 'executable': False}
        reserved = False
        key = None
        try:
            auth = self.repo.get_state('apollo_linkedin_canary', authorization_id)
            if (not isinstance(auth, dict) or auth.get('task_id') != task_id
                    or auth.get('state') != 'issued' or auth.get('action_type') != 'linkedin_step_message'):
                return {'state': 'blocked', 'reason': 'canary_unavailable'}
            approval = self.repo.get_state('approval', auth['approval_id'])
            if not isinstance(approval, dict) or not isinstance(approval.get('campaign_id'), str):
                return {'state': 'blocked', 'reason': 'campaign_unapproved'}
            campaign = approval['campaign_id']
            version = auth['campaign_version']
            approved = self.authority.require(campaign, auth['approval_id'], version)
            binding = approved['binding']
            if (binding['company_id'] != auth['company'] or binding['account_id'] != auth['account_id']
                    or binding['public_id'].lower() == auth['recipient'].lower()):
                return {'state': 'blocked', 'reason': 'binding_mismatch'}
            with self.repo.apollo_linkedin_account_lock(binding['account_id']):
                if not self._gate():
                    return {'state': 'blocked', 'reason': 'execution_disabled'}
                task = self.apollo.get(task_id)
                text = self._task(task, task_id, auth, binding)
                contact = task['contact']
                if (not isinstance(contact.get('id'), str) or not contact['id']
                        or not isinstance(contact.get('email'), str) or not contact['email']):
                    return {'state': 'blocked', 'reason': 'contact_unverified'}
                target = {'email': contact['email'], 'apollo_contact_id': contact['id'],
                          'sequence_id': binding['sequence_id'], 'linkedin_profile_urn': auth['member_urn']}
                plan = {'account_id': binding['account_id'], 'account_public_id': binding['public_id'],
                        'action': 'dm', 'recipient_public_id': auth['recipient'],
                        'recipient_profile_urn': auth['member_urn'], 'channel': 'linkedin',
                        'messaging_channel': 'linkedin_inbox', 'content': text}
                if not self.message.relationship(plan):
                    return {'state': 'blocked', 'reason': 'not_connected_or_unknown'}
                if self.safety.other_writers_inactive() is not True or self.safety.ready(binding, plan) is not True:
                    return {'state': 'blocked', 'reason': 'safety_unverified'}
                observed = self.coverage.observe(target)
                if observed.get('status') == 'reply':
                    paused = self.safety.pause_after_reply(binding, plan)
                    return {'state': 'blocked', 'reason': 'human_reply' if isinstance(paused, dict)
                            and paused.get('persisted') is True else 'human_reply_pause_unverified'}
                if not coverage_current(observed, datetime.now(timezone.utc),
                                        hubspot_configured=self.coverage.hubspot_configured):
                    return {'state': 'blocked', 'reason': 'coverage_unavailable'}
                key = (auth['company'], task_id, 'linkedin_step_message')
                self.repo.reserve_apollo_linkedin_with_canary(authorization_id, *key, binding['account_id'],
                    auth['recipient'], auth['text_hash'], version,
                    member_urn=auth['member_urn'], approval_id=auth['approval_id'])
                reserved = True
                reserved_at = self.clock()
                # BEGIN IMMEDIATE serializes pause and approval updates in the
                # official DB until the single dispatch outcome is persisted.
                with self.repo.transaction():
                    stop_reason = []
                    def before_dispatch():
                        if not self._gate() or self.safety.other_writers_inactive() is not True:
                            return False
                        self.authority.require(campaign, auth['approval_id'], version)
                        current = self.apollo.get(task_id)
                        self._task(current, task_id, auth, binding)
                        if (current['contact'].get('id') != target['apollo_contact_id']
                                or current['contact'].get('email') != target['email']):
                            return False
                        latest = self.coverage.observe(target)
                        if latest.get('status') == 'reply':
                            paused = self.safety.pause_after_reply(binding, plan)
                            stop_reason.append('human_reply' if isinstance(paused, dict)
                                               and paused.get('persisted') is True else 'human_reply_pause_unverified')
                            return False
                        return (coverage_current(latest, datetime.now(timezone.utc),
                                hubspot_configured=self.coverage.hubspot_configured)
                                and self.safety.ready(binding, plan) is True)
                    if not before_dispatch():
                        result = {'status': 'blocked', 'reason': 'execution_disabled'}
                    else:
                        result = self.message.send(plan, reserved_at, before_dispatch=before_dispatch)
                    if stop_reason and result.get('status') == 'blocked':
                        result = {'status': 'blocked', 'reason': stop_reason[-1]}
                    if result.get('status') == 'sent' and isinstance(result.get('receipt_ref'), str) and result['receipt_ref']:
                        receipt = {'provider_id': result['receipt_ref']}
                        self.repo.finish_apollo_linkedin(*key, 'confirmed', None, receipt=receipt, apollo_status='pending')
                        state = 'confirmed'
                    elif result.get('status') == 'blocked':
                        self.repo.finish_apollo_linkedin(*key, 'rejected', result.get('reason', 'dispatch_blocked'))
                        state = 'rejected'
                    else:
                        self.repo.finish_apollo_linkedin(*key, 'uncertain', 'receipt_unverified')
                        state = 'uncertain'
                if state != 'confirmed':
                    return {'state': state, 'reason': result.get('reason')}
                try:
                    done = self.completion.complete_verified(*key, campaign_id=campaign,
                        approval_id=auth['approval_id'], campaign_version=version)
                    return {'state': 'confirmed' if isinstance(done, dict) and done.get('state') == 'confirmed'
                            else 'apollo_reconcile_pending'}
                except Exception:
                    return {'state': 'apollo_reconcile_pending', 'reason': 'apollo_completion_unverified'}
        except Exception:
            # An exception after reservation can reflect an ambiguous POST or
            # commit. Preserve the durable reservation and never retry it.
            if reserved:
                try:
                    self.repo.finish_apollo_linkedin(*key, 'uncertain', 'execution_unverified')
                except Exception:
                    pass
                return {'state': 'uncertain', 'reason': 'execution_unverified'}
            return {'state': 'blocked', 'reason': 'preflight_unavailable'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Apollo LinkedIn canary (disabled)')
    parser.add_argument('--task', required=True)
    parser.add_argument('--authorization', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    result = execution_status(args.task, args.authorization)
    result['mode'] = 'execute_requested' if args.execute else 'dry_run'
    print(json.dumps(result, ensure_ascii=False))
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
