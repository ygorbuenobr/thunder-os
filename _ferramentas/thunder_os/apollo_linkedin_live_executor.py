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
import sqlite3
from pathlib import Path
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone

from .apollo_linkedin_attention import EXCLUDED_TASK
from .apollo_linkedin_preview import _slug, validated_task_text
from .apollo_linkedin_reply_coverage import coverage_current
from .apollo_linkedin_completion import write_completion_guard

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


def valid_campaign_version(value):
    # The reservation contract currently bounds versions to 128 characters.
    return (isinstance(value, str) and bool(value.strip()) and value == value.strip()
            and len(value) <= 128)


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
                 safety, completion, clock=time.time, target_resolver=None, invitation=None):
        self.repo, self.authority, self.apollo = repository, authority, apollo
        self.message, self.coverage, self.safety = message, coverage, safety
        self.completion, self.clock = completion, clock
        self.target_resolver = target_resolver
        self.invitation = invitation

    def _gate(self):
        control = self.repo.get_state('control', 'global')
        return (os.environ.get('THUNDER_APOLLO_LI_EXECUTION_ENABLED') == 'true'
                and isinstance(control, dict) and control.get('paused') is False)

    @staticmethod
    def _task(task, task_id, auth, binding):
        action_type = auth.get('action_type', 'linkedin_step_message')
        if (not isinstance(task, dict) or task.get('id') != task_id
                or action_type not in ('linkedin_step_message', 'linkedin_step_connect')
                or task.get('type') != action_type or task.get('status') != 'scheduled'
                or task.get('emailer_campaign_id') != binding['sequence_id']
                or task.get('user_id') not in binding['owner_ids']):
            raise ValueError('task_binding_changed')
        contact = task.get('contact')
        if (not isinstance(contact, dict) or not isinstance(contact.get('id'), str)
                or task.get('contact_id') != contact['id']
                or _slug(contact.get('linkedin_url')) != auth['recipient']):
            raise ValueError('recipient_changed')
        text, digest, error = validated_task_text(task, 'invite' if action_type == 'linkedin_step_connect' else 'message')
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
        try:
            auth = self.repo.get_state('apollo_linkedin_canary', authorization_id)
            if (not isinstance(auth, dict) or auth.get('task_id') != task_id
                    or auth.get('state') != 'issued' or auth.get('action_type') not in ('linkedin_step_message', 'linkedin_step_connect')):
                return {'state': 'blocked', 'reason': 'canary_unavailable'}
            approval = self.repo.get_state('approval', auth['approval_id'])
            if not isinstance(approval, dict) or not isinstance(approval.get('campaign_id'), str):
                return {'state': 'blocked', 'reason': 'campaign_unapproved'}
            campaign = approval['campaign_id']
            version = auth['campaign_version']
            return self._run_bound(task_id, auth, campaign, version, canary_id=authorization_id)
        except Exception:
            return {'state': 'blocked', 'reason': 'preflight_unavailable'}

    def run_campaign(self, task_id, campaign_id, approval_id, campaign_version, *, dry_run=True):
        """Execute one task under a persisted campaign approval, never a caller-supplied target."""
        if task_id == EXCLUDED_TASK:
            return {'state': 'blocked', 'reason': 'fixture_protegida'}
        if (not all(valid_id(value) for value in (task_id, campaign_id, approval_id))
                or not valid_campaign_version(campaign_version)):
            return {'state': 'blocked', 'reason': 'configuration_invalid'}
        if dry_run is not False:
            return {'state': 'dry_run', 'executable': False}
        try:
            if not self._gate():
                return {'state': 'blocked', 'reason': 'execution_disabled'}
            approved = self.authority.require(campaign_id, approval_id, campaign_version)
            binding = approved['binding']
            if (self.apollo.team_id != binding['team_id']
                    or self.apollo.owner_ids != frozenset(binding['owner_ids'])):
                return {'state': 'blocked', 'reason': 'reader_binding_mismatch'}
            task = self.apollo.get(task_id)
            contact = task.get('contact') if isinstance(task, dict) else None
            recipient = _slug(contact.get('linkedin_url')) if isinstance(contact, dict) else None
            action_type = task.get('type')
            if action_type not in ('linkedin_step_message', 'linkedin_step_connect'):
                return {'state': 'blocked', 'reason': 'task_unverified'}
            _, digest, error = validated_task_text(task, 'invite' if action_type == 'linkedin_step_connect' else 'message')
            if (error or not recipient
                    or task.get('status') != 'scheduled'):
                return {'state': 'blocked', 'reason': 'task_unverified'}
            resolver = self.target_resolver
            if resolver is None:
                from .connectsafely_read_targets import build_read_resolver
                resolver = build_read_resolver(self.repo)
            if not callable(resolver):
                return {'state': 'blocked', 'reason': 'resolver_unavailable'}
            from .apollo_linkedin_identity_binding import create_identity_binding
            identity = create_identity_binding(self.repo, reader=self.apollo, resolver=resolver,
                campaign_id=campaign_id, approval_id=approval_id,
                campaign_version=campaign_version, task_id=task_id)
            if (identity.get('company') != binding['company_id']
                    or identity.get('account_id') != binding['account_id']
                    or identity.get('scope_hash') != approved['scope_hash']
                    or identity.get('campaign_id') != campaign_id
                    or identity.get('approval_id') != approval_id
                    or identity.get('campaign_version') != campaign_version
                    or identity.get('team_id') != binding['team_id']
                    or identity.get('sequence_id') != binding['sequence_id']
                    or identity.get('task_id') != task_id
                    or identity.get('action_type') != task['type']
                    or identity.get('contact_id') != contact.get('id')
                    or identity.get('recipient_public_id') != recipient):
                return {'state': 'blocked', 'reason': 'recipient_unverified'}
            auth = {'approval_id': approval_id, 'campaign_version': campaign_version,
                    'company': binding['company_id'], 'account_id': binding['account_id'],
                    'recipient': recipient, 'member_urn': identity['member_urn'],
                    'text_hash': digest, 'contact_id': contact.get('id'), 'binding_id': identity['id'],
                    'action_type': action_type}
            return self._run_bound(task_id, auth, campaign_id, campaign_version, canary_id=None)
        except Exception:
            return {'state': 'blocked', 'reason': 'preflight_unavailable'}

    def _run_bound(self, task_id, auth, campaign, version, *, canary_id):
        reserved = False
        key = None
        try:
            approved = self.authority.require(campaign, auth['approval_id'], version)
            binding = approved['binding']
            action_type = auth.get('action_type', 'linkedin_step_message')
            is_invite = action_type == 'linkedin_step_connect'
            adapter = self.invitation if is_invite else self.message
            if adapter is None:
                return {'state': 'blocked', 'reason': 'action_unavailable'}
            if is_invite:
                binding = {**binding, '_linkedin_policy': approved.get('configuration', {}).get('linkedin_policy')}
            if (binding['company_id'] != auth['company'] or binding['account_id'] != auth['account_id']
                    or binding['public_id'].lower() == auth['recipient'].lower()):
                return {'state': 'blocked', 'reason': 'binding_mismatch'}
            with self.repo.apollo_linkedin_account_lock(binding['account_id']):
                if not self._gate():
                    return {'state': 'blocked', 'reason': 'execution_disabled'}
                task = self.apollo.get(task_id)
                text = self._task(task, task_id, auth, binding)
                contact = task['contact']
                if canary_id is None and contact.get('id') != auth.get('contact_id'):
                    return {'state': 'blocked', 'reason': 'contact_changed'}
                if (not isinstance(contact.get('id'), str) or not contact['id']
                        or (contact.get('email') is not None and not isinstance(contact.get('email'), str))):
                    return {'state': 'blocked', 'reason': 'contact_unverified'}
                target = {'email': contact.get('email'), 'apollo_contact_id': contact['id'],
                          'sequence_id': binding['sequence_id'], 'linkedin_profile_urn': auth['member_urn'],
                          'account_id': binding['account_id'], 'recipient_public_id': auth['recipient']}
                plan = {'account_id': binding['account_id'], 'account_public_id': binding['public_id'],
                        'action': 'invite' if is_invite else 'dm', 'recipient_public_id': auth['recipient'],
                        'recipient_profile_urn': auth['member_urn'], 'channel': 'linkedin',
                        'messaging_channel': 'linkedin_inbox', 'content': text}
                bind_plan = getattr(self.coverage, 'bind_plan', None)
                if callable(bind_plan):
                    bind_plan(plan)
                if not adapter.relationship(plan):
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
                key = (auth['company'], task_id, action_type)
                if canary_id is None:
                    self.repo.reserve_apollo_linkedin_for_campaign(campaign, auth['approval_id'], version,
                        task, observed_team_id=self.apollo.team_id, expected_text_hash=auth['text_hash'],
                        binding_id=auth['binding_id'])
                else:
                    self.repo.reserve_apollo_linkedin_with_canary(canary_id, *key, binding['account_id'],
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
                        result = adapter.send(plan, reserved_at, before_dispatch=before_dispatch)
                    if stop_reason and result.get('status') == 'blocked':
                        result = {'status': 'blocked', 'reason': stop_reason[-1]}
                    if result.get('status') == 'sent' and isinstance(result.get('receipt_ref'), str) and result['receipt_ref']:
                        receipt = {'provider_id': result['receipt_ref']}
                        write_completion_guard(self.repo, *key, receipt['provider_id'], 'pending')
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
                # A reply may arrive while the provider POST is in flight. The
                # receipt is already durable; never repeat the LinkedIn action
                # and do not advance Apollo until fresh coverage is known.
                try:
                    after = self.coverage.observe(target)
                    if after.get('status') == 'reply':
                        paused = self.safety.pause_after_reply(binding, plan)
                        write_completion_guard(self.repo, *key, receipt['provider_id'], 'reply')
                        return {'state': 'apollo_reconcile_pending',
                                'reason': 'human_reply_after_dispatch' if isinstance(paused, dict)
                                and paused.get('persisted') is True else 'human_reply_pause_unverified'}
                    if not coverage_current(after, datetime.now(timezone.utc),
                                            hubspot_configured=self.coverage.hubspot_configured):
                        write_completion_guard(self.repo, *key, receipt['provider_id'], 'unknown')
                        return {'state': 'apollo_reconcile_pending', 'reason': 'coverage_after_dispatch_unavailable'}
                    write_completion_guard(self.repo, *key, receipt['provider_id'], 'clear')
                except Exception:
                    return {'state': 'apollo_reconcile_pending', 'reason': 'coverage_after_dispatch_unavailable'}
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


def runtime_preflight(task, authorization=None):
    """Local, read-only checklist. An unknown observation never authorizes a send."""
    checks = {'task': 'unknown', 'campaign_approval': 'unknown', 'canary': 'unknown',
              'account': 'unknown', 'relationship': 'unknown', 'quota': 'unknown',
              'pacing': 'unknown', 'reply_coverage': 'unknown', 'other_writers': 'unknown',
              'global_pause': 'unknown', 'execution_key': 'enabled' if
              os.environ.get('THUNDER_APOLLO_LI_EXECUTION_ENABLED') == 'true' else 'disabled'}
    result = {'state': 'dry_run', 'executable': False, 'checks': checks}
    if not valid_id(task) or (authorization is not None and not valid_id(authorization)):
        return {**result, 'state': 'blocked', 'reason': 'configuration_invalid'}
    if task == EXCLUDED_TASK:
        return {**result, 'state': 'blocked', 'reason': 'fixture_protegida'}
    checks['task'] = 'not_observed'
    checks['other_writers'] = 'inactive' if hermes_unit_inactive() else 'unknown_or_active'
    path = os.environ.get('PAINEL_DB', '')
    try:
        db = Path(path)
        if not db.is_absolute() or not db.is_file():
            raise ValueError('official_store_unavailable')
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as con:
            con.execute('PRAGMA query_only=ON')
            row = con.execute("SELECT payload FROM thunder_state WHERE kind='control' AND entity_id='global'").fetchone()
            if row:
                control = json.loads(row[0])
                if type(control.get('paused')) is bool:
                    checks['global_pause'] = 'paused' if control['paused'] else 'unpaused'
            if authorization is None:
                rows = con.execute("SELECT entity_id,payload FROM thunder_state WHERE kind='apollo_linkedin_canary' LIMIT 1001").fetchall()
                if len(rows) <= 1000:
                    matches = []
                    now = datetime.now(timezone.utc)
                    for ident, payload in rows:
                        try:
                            value = json.loads(payload)
                            issued = datetime.fromisoformat(value['issued_at'])
                            expires = datetime.fromisoformat(value['expires_at'])
                            if (value.get('task_id') == task and value.get('state') == 'issued'
                                    and value.get('action_type') in ('linkedin_step_message', 'linkedin_step_connect')
                                    and issued.tzinfo is not None and expires.tzinfo is not None
                                    and issued <= now < expires and timedelta(0) < expires - issued <= timedelta(minutes=30)):
                                matches.append((ident, value))
                        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                            continue
                    if len(matches) == 1:
                        authorization = matches[0][0]
                        result['authorization_id'] = authorization
            if authorization:
                row = con.execute("SELECT payload FROM thunder_state WHERE kind='apollo_linkedin_canary' AND entity_id=?", (authorization,)).fetchone()
                if row:
                    canary = json.loads(row[0])
                    if canary.get('task_id') == task and canary.get('state') == 'issued':
                        checks['canary'] = 'observed_unverified'
        checks['official_store'] = 'observed'
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        checks['official_store'] = 'unavailable'
    if authorization and checks.get('canary') == 'observed_unverified':
        try:
            coordinator = build_runtime(task, authorization, readonly=True)
            auth = coordinator.repo.get_state('apollo_linkedin_canary', authorization)
            approval = coordinator.repo.get_state('approval', auth['approval_id'])
            approved = coordinator.authority.require(approval['campaign_id'], auth['approval_id'], auth['campaign_version'])
            binding = approved['binding']
            is_invite = auth.get('action_type') == 'linkedin_step_connect'
            if is_invite:
                binding = {**binding, '_linkedin_policy': approved.get('configuration', {}).get('linkedin_policy')}
            task_data = coordinator.apollo.get(task)
            content = coordinator._task(task_data, task, auth, binding)
            contact = task_data['contact']
            plan = {'account_id': binding['account_id'], 'account_public_id': binding['public_id'],
                    'action': 'invite' if is_invite else 'dm', 'recipient_public_id': auth['recipient'],
                    'recipient_profile_urn': auth['member_urn'], 'channel': 'linkedin',
                    'messaging_channel': 'linkedin_inbox', 'content': content}
            coordinator.coverage.bind_plan(plan)
            target = {'email': contact.get('email'), 'apollo_contact_id': contact['id'],
                      'sequence_id': binding['sequence_id'], 'linkedin_profile_urn': auth['member_urn'],
                      'account_id': binding['account_id'], 'recipient_public_id': auth['recipient']}
            checks['task'] = 'verified'
            checks['campaign_approval'] = 'verified'
            checks['account'] = 'verified'
            adapter = coordinator.invitation if is_invite else coordinator.message
            checks['relationship'] = ('eligible' if is_invite else 'connected') if adapter and adapter.relationship(plan) else 'unknown_or_ineligible'
            checks['quota_and_pacing'] = 'ready' if coordinator.safety.ready(binding, plan) else 'blocked_or_unknown'
            checks['quota'] = checks['quota_and_pacing']
            checks['pacing'] = checks['quota_and_pacing']
            observed = coordinator.coverage.observe(target)
            checks['reply_coverage'] = ('clear' if coverage_current(observed, datetime.now(timezone.utc),
                hubspot_configured=coordinator.coverage.hubspot_configured) else observed.get('status', 'unknown'))
            expiry = datetime.fromisoformat(auth['expires_at'])
            issued = datetime.fromisoformat(auth['issued_at'])
            now = datetime.now(timezone.utc)
            from .db import Repository
            Repository._t4_canary_fields(auth['company'], task, auth['account_id'], auth['recipient'], auth['text_hash'])
            Repository._t4_message_binding(auth['member_urn'], auth['campaign_version'], auth['approval_id'])
            fields = {'company', 'task_id', 'account_id', 'recipient', 'text_hash', 'actor', 'issued_at',
                      'expires_at', 'state', 'action_type', 'member_urn', 'campaign_version', 'approval_id',
                      'id', 'version', 'updated_at'}
            if (set(auth) != fields or type(auth.get('version')) is not int or auth['version'] < 1
                    or auth.get('id') != authorization or not re.fullmatch(r'[a-f0-9]{32}', authorization)
                    or not isinstance(auth.get('actor'), str)
                    or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}', auth['actor'])):
                raise ValueError('canary_actor_invalid')
            checks['canary'] = ('valid' if expiry.tzinfo is not None and issued.tzinfo is not None
                                and issued <= now < expiry and timedelta(0) < expiry - issued <= timedelta(minutes=30)
                                and auth.get('action_type') in ('linkedin_step_message', 'linkedin_step_connect')
                                else 'expired')
            checks['other_writers'] = ('inactive' if coordinator.safety.other_writers_inactive()
                                       else 'unknown_or_active')
            checks['executable_after_enable'] = all((checks['task'] == 'verified',
                checks['campaign_approval'] == 'verified', checks['account'] == 'verified',
                checks['relationship'] == ('eligible' if is_invite else 'connected'), checks['quota_and_pacing'] == 'ready',
                checks['reply_coverage'] == 'clear', checks['canary'] == 'valid',
                checks['other_writers'] == 'inactive', checks['global_pause'] == 'unpaused'))
        except Exception:
            checks['read_only_observation'] = 'unavailable'
    return result


class _RuntimeCoverage:
    """Bind the conversation source to the exact plan under review."""
    def __init__(self, apollo, message, safety, *, hubspot_configured=False):
        from .apollo_linkedin_reply_coverage import ReplyCoverage
        self.message, self.safety, self.plan = message, safety, None
        if type(hubspot_configured) is not bool:
            raise ValueError('hubspot_configuration_invalid')
        self.hubspot_configured = hubspot_configured
        # An approved CRM is mandatory coverage. Until a tenant-scoped contact,
        # company and history binding exists, do not silently pretend it is absent.
        self.delegate = ReplyCoverage(apollo, self._linkedin, hubspot_configured=hubspot_configured)

    def bind_plan(self, plan):
        self.plan = dict(plan)

    def _linkedin(self, profile_urn):
        if self.plan is None:
            raise ValueError('plan_unavailable')
        return self.message.reply_source(self.plan, self.safety.last_touch_at(self.plan))(profile_urn)

    def observe(self, target):
        return self.delegate.observe(target)


def _approved_hubspot_required(approval):
    """Read CRM scope only from the hash-verified campaign approval context."""
    context = approval.get('context') if isinstance(approval, dict) else None
    providers = context.get('providers') if isinstance(context, dict) else None
    if not isinstance(providers, dict):
        raise ValueError('campaign_providers_unverified')
    if 'hubspot' in providers and providers['hubspot'] != 'hubspot':
        raise ValueError('campaign_crm_unsupported')
    return 'hubspot' in providers


def build_runtime(task, authorization, *, readonly=False, campaign_id=None,
                  approval_id=None, campaign_version=None):
    """Build the exact approved account's ports from explicitly configured inputs."""
    from .db import Repository, _OwnedConnection, _BorrowedConnection, _official_apollo_linkedin_db
    from .apollo_linkedin_authority import CampaignApprovalAuthority
    from .apollo_linkedin_reader import ApolloLinkedInReader
    from .apollo_linkedin_completion import ApolloLinkedInCompletion
    from .apollo_linkedin_reply_coverage import ApolloContactReplyReader
    from .apollo_linkedin_message import MessageAdapter
    from .apollo_linkedin_invitation import InvitationAdapter
    from .apollo_linkedin_runtime_safety import RuntimeSafety
    from .connectsafely_live import build_panel_composition, credential
    from .credential_paths import credential_path
    campaign_mode = campaign_id is not None
    if campaign_mode:
        if (task is not None or authorization is not None
                or not all(valid_id(value) for value in (campaign_id, approval_id))
                or not valid_campaign_version(campaign_version)):
            raise ValueError('configuration_invalid')
    elif (not valid_id(task) or task == EXCLUDED_TASK or not valid_id(authorization)
          or approval_id is not None or campaign_version is not None):
        raise ValueError('configuration_invalid')
    path = Path(os.environ.get('PAINEL_DB', ''))
    if (not path.is_absolute() or not path.is_file()
            or path.resolve() != _official_apollo_linkedin_db().resolve()):
        raise ValueError('official_store_unavailable')
    class ReadOnlyRepository(Repository):
        def migrate(self):
            return None

        def _connect(self):
            active = getattr(self._local, 'connection', None)
            if active is not None:
                return _BorrowedConnection(active)
            con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5,
                                  isolation_level=None, factory=_OwnedConnection)
            con.row_factory = sqlite3.Row
            con.execute('PRAGMA query_only=ON')
            return con

        @contextmanager
        def transaction(self):
            active = getattr(self._local, 'connection', None)
            if active is not None:
                yield active
                return
            con = self._connect()
            try:
                con.execute('BEGIN')
                self._local.connection = con
                yield con
            finally:
                con.rollback()
                self._local.connection = None
                con.close()

    repo = ReadOnlyRepository(str(path)) if readonly else Repository(str(path))
    repo.apollo_linkedin_store_ready()
    authority = CampaignApprovalAuthority(repo)
    if campaign_mode:
        approved = authority.require(campaign_id, approval_id, campaign_version)
        binding = approved['binding']
        canary = None
    else:
        canary = repo.get_state('apollo_linkedin_canary', authorization)
        if not isinstance(canary, dict) or canary.get('task_id') != task or canary.get('state') != 'issued':
            raise ValueError('canary_unavailable')
        approval = repo.get_state('approval', canary['approval_id'])
        if not isinstance(approval, dict) or not isinstance(approval.get('campaign_id'), str):
            raise ValueError('campaign_unapproved')
        approved = authority.require(approval['campaign_id'], canary['approval_id'], canary['campaign_version'])
        binding = approved['binding']
    hubspot_configured = _approved_hubspot_required(approved)
    actor = os.environ.get('THUNDER_APOLLO_LI_API_ACTOR', '')
    if ((canary is not None and (binding['company_id'] != canary['company'] or binding['account_id'] != canary['account_id']))
            or os.environ.get('THUNDER_APOLLO_LI_COMPANY') != binding['company_id']
            or os.environ.get('THUNDER_APOLLO_LI_TEAM') != binding['team_id'] or not valid_id(actor)):
        raise ValueError('runtime_binding_mismatch')
    config = credential_path('connectsafely_config', os.environ.get('THUNDER_OS_CONNECTSAFELY_CONFIG', ''))
    composition = build_panel_composition(repo, config_path=config,
        config_sha256=os.environ.get('THUNDER_OS_CONNECTSAFELY_CONFIG_SHA256', ''))
    app = composition.application
    provider = app.provider
    client = getattr(provider, 'direct_client', None)
    ledger = getattr(app.direct, 'ledger', None) if app.direct is not None else None
    if (client is None or ledger is None or client.account_id != binding['account_id']
            or client.expected_public_id != binding['public_id']):
        raise ValueError('connectsafely_binding_unavailable')
    loader = credential(credential_path('apollo_api_key', '/root/.config/thunder/credentials/apollo_api_key.txt'))
    apollo = ApolloLinkedInReader(loader, team_id=binding['team_id'], api_actor_id=actor,
                                   owner_ids=binding['owner_ids'])
    contact_reader = ApolloContactReplyReader(loader, team_id=binding['team_id'], api_actor_id=actor)
    message = MessageAdapter(client)
    safety = RuntimeSafety(repo, client, ledger, unit_check=hermes_unit_inactive)
    coverage = _RuntimeCoverage(contact_reader, message, safety, hubspot_configured=hubspot_configured)
    completion = None if readonly else ApolloLinkedInCompletion(loader, team_id=binding['team_id'], actor_id=actor,
        repository=repo, enabled=os.environ.get('THUNDER_APOLLO_LI_EXECUTION_ENABLED') == 'true',
        coverage=coverage, safety=safety)
    return MessageCoordinator(repository=repo, authority=authority, apollo=apollo,
        message=message, coverage=coverage, safety=safety, completion=completion,
        invitation=InvitationAdapter(client))


def build_campaign_runtime(campaign_id, approval_id, campaign_version, *, readonly=False):
    """Build a scoped coordinator from persisted approval, without issuing a canary."""
    return build_runtime(None, None, readonly=readonly, campaign_id=campaign_id,
                         approval_id=approval_id, campaign_version=campaign_version)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Apollo LinkedIn canary (disabled)')
    parser.add_argument('--task', required=True)
    parser.add_argument('--authorization', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    if not args.execute:
        result = runtime_preflight(args.task, args.authorization)
        result['mode'] = 'dry_run'
    elif os.environ.get('THUNDER_APOLLO_LI_EXECUTION_ENABLED') != 'true':
        result = execution_status(args.task, args.authorization)
        result['state'] = result['status']
        result['mode'] = 'execute_requested'
    else:
        try:
            result = build_runtime(args.task, args.authorization).run(args.task, args.authorization, dry_run=False)
        except Exception:
            result = {'state': 'blocked', 'reason': 'runtime_unavailable', 'executable': False}
        result['mode'] = 'execute_requested'
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('state') in ('confirmed', 'dry_run') else 2


if __name__ == '__main__':
    raise SystemExit(main())
