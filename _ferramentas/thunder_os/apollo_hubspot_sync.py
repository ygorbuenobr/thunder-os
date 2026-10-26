"""Bounded factual Apollo snapshot -> CRM logs. No sends or implicit activation.

permission_check() must return 'AVAILABLE' (or {status:'AVAILABLE',
portal_id:PORTAL_ID}) from the current EMAIL-create capability check. This is
separate from authorize(plan), the existing human/delegated authorization.
Snapshots are trusted reader outputs, not client-submitted attestations. Callers
own reader freshness/credential provenance; stale/partial snapshots fail closed.
Plans and bindings are private runtime data in the existing Repository only.
"""
from collections.abc import Mapping
from copy import copy, deepcopy
from threading import Lock

from .external_email_import import (
    APOLLO_TEAM, APOLLO_SEQUENCE, PORTAL_ID, ExternalEmailError,
    ExternalEmailImport, HubSpotExternalEmail, _validate_plan, contact_emails,
    email, identifier, plan_contact, plan_email, require, source_identity, timestamp,
)
from .models import payload_hash
from .seqd_client import MAILBOX

CAMPAIGN_ID = 'BR-APOLLO-01'
BINDING_KIND = 'apollo_hubspot_binding'
MAX_EVENTS_PER_CONTACT = 100
# Complete sent enumeration/content may coexist with an unavailable reply original.
# This exception admits source facts only; it never grants EMAIL-write permission.
REPLY_ONLY_PARTIAL_REASONS = frozenset({
    'reply_original_unavailable', 'gmail_reply_read_unavailable',
    'gmail_reply_discovery_unavailable', 'gmail_discovery_page_limit',
    'gmail_reply_body_partial',
})


def sync_source(value):
    """Apollo source or original Gmail message in the pinned Apollo campaign."""
    if isinstance(value, Mapping) and value.get('provider') == 'gmail':
        require(set(value) == {'provider', 'account_id', 'external_campaign_id',
                              'campaign_id', 'provider_id'}, 'source_identity_mismatch')
        require(value['account_id'] == MAILBOX and value['external_campaign_id'] == APOLLO_SEQUENCE
                and value['campaign_id'] == CAMPAIGN_ID
                and identifier(value['provider_id']), 'source_identity_mismatch')
        return dict(value)
    return source_identity(value)


def validate_sync_plan(plan):
    _validate_plan(plan, source_validator=sync_source)
    if plan['object_type'] == 'emails':
        source = plan['source']
        require(source['campaign_id'] == CAMPAIGN_ID, 'source_identity_mismatch')
        require(source['provider'] != 'gmail' or plan['event_type'] == 'reply_received',
                'gmail_reply_only')


class _GuardedTransport:
    def __init__(self, owner):
        self.owner = owner
        # Use the existing injectable validator without mutating the shared port.
        self.port = copy(owner.transport) if isinstance(owner.transport, HubSpotExternalEmail) else owner.transport
        if isinstance(self.port, HubSpotExternalEmail):
            original_validator = self.port.validate_plan
            def validate(plan):
                validate_sync_plan(plan)
                # Extend the stock source validator only. Preserve any narrower
                # runtime validator instead of widening a canary-specific policy.
                if original_validator is not _validate_plan:
                    original_validator(plan)
            self.port.validate_plan = validate

    def __getattr__(self, name):
        return getattr(self.port, name)

    def write(self, plan):
        require(self.owner._authorize(plan), 'email_write_not_authorized')
        self.owner._writes += 1
        return self.port.write(plan)


class ApolloHubSpotSync:
    """One explicit bounded run; no scheduler, credentials, tables or approvals.

    run(..., cursor={snapshot_hash, selection_hash, offset}) continues the exact same snapshot.
    max_contacts is 1..10; exact_emails optionally selects exact primary/aliases.
    A contact with >100 events is retained whole as event_limit, never truncated.
    readiness() is informational; permission/authorization are rechecked at writes.
    """
    def __init__(self, repo, transport, *, enabled=False, authorize=None, permission_check=None):
        self.repo, self.transport = repo, transport
        self.enabled, self.authorize, self.permission_check = enabled, authorize, permission_check
        self._writes = 0
        self._run_lock = Lock()

    def readiness(self):
        permission = 'UNAVAILABLE'
        if callable(self.permission_check):
            try:
                result = self.permission_check()
                if isinstance(result, Mapping):
                    if str(result.get('portal_id')) == PORTAL_ID:
                        permission = result.get('status', 'UNAVAILABLE')
                elif isinstance(result, str):
                    permission = result
            except Exception:
                permission = 'UNAVAILABLE'
        # Only the exact availability state grants admission. Never expose arbitrary errors.
        if not isinstance(permission, str) or permission not in {'AVAILABLE', 'REQUIRES_PERMISSION_MODIFICATION', 'UNAVAILABLE'}:
            permission = 'UNAVAILABLE'
        transport_ready = not isinstance(self.transport, HubSpotExternalEmail) or (
            self.transport.enabled is True and callable(self.transport.authorize))
        ready = self.enabled is True and callable(self.authorize) and transport_ready and permission == 'AVAILABLE'
        return {'ready': ready, 'enabled': self.enabled is True, 'portal_id': PORTAL_ID,
                'email_write_permission': permission, 'transport_write_enabled': transport_ready, 'sends_enabled': False,
                'enrollment_enabled': False, 'agent_activation_enabled': False}

    def _authorize(self, plan):
        if not self.readiness()['ready']:
            return False
        try:
            return self.authorize(deepcopy(plan)) is True
        except Exception:
            return False

    @staticmethod
    def _scope(snapshot):
        require(isinstance(snapshot, Mapping), 'invalid_snapshot')
        require((snapshot.get('campaign_id'), snapshot.get('account_id'), snapshot.get('external_campaign_id'))
                == (CAMPAIGN_ID, APOLLO_TEAM, APOLLO_SEQUENCE), 'source_identity_mismatch')
        require(str(snapshot.get('portal_id', PORTAL_ID)) == PORTAL_ID, 'portal_mismatch')
        timestamp(snapshot.get('observed_at'))
        coverage = snapshot.get('coverage', {})
        require(isinstance(coverage, Mapping) and all(coverage.get(k) is True for k in
                ('contacts_complete', 'messages_complete')), 'incomplete_source')
        reasons = coverage.get('reasons')
        reply_only_partial = (coverage.get('complete') is False and snapshot.get('status') == 'partial'
            and coverage.get('contents_complete') is True and isinstance(reasons, list) and bool(reasons)
            and all(isinstance(reason, str) and reason in REPLY_ONLY_PARTIAL_REASONS for reason in reasons))
        require((coverage.get('complete') is True and snapshot.get('status') != 'partial')
                or reply_only_partial, 'incomplete_source')
        require(snapshot.get('status') not in {'stale', 'error', 'unavailable', 'disabled', 'pending'}
                and snapshot.get('stale') is not True and snapshot.get('enabled') is not False, 'incomplete_source')
        leads = snapshot.get('leads')
        require(isinstance(leads, list) and len(leads) <= 10000, 'invalid_leads')
        seen = set()
        for lead in leads:
            require(isinstance(lead, Mapping) and identifier(lead.get('id'))
                    and lead['id'] not in seen, 'invalid_source_contact')
            seen.add(lead['id'])
        return leads

    @staticmethod
    def _binding_key(lead):
        return payload_hash({'team': APOLLO_TEAM, 'sequence': APOLLO_SEQUENCE, 'contact': lead['id']})

    @staticmethod
    def _contact_source(lead):
        # Name is preserved as context only; never split a display name or invent fields.
        return {k: deepcopy(lead.get(k)) for k in
                ('id', 'name', 'first_name', 'last_name', 'email', 'contact_emails', 'company', 'title', 'linkedin_url')}

    def _original(self, plan, fingerprint):
        """Recover saved create plan even when the new snapshot now says existing."""
        effect, keys = ExternalEmailImport._keys(plan)
        state = self.repo.get_state('hubspot_attempt', effect)
        if state is None:
            locks = [self.repo.get_state('hubspot_identity', key) for key in keys]
            effects = {lock['effect'] for lock in locks if lock}
            require(len(effects) <= 1, 'identity_has_pending_attempt')
            if effects:
                state = self.repo.get_state('hubspot_attempt', effects.pop())
                require(state is not None, 'attempt_not_found')
        if state:
            saved = state.get('plan', {})
            require(saved.get('sync_source_hash') == fingerprint, 'sync_source_changed')
            validate_sync_plan(saved)
            require(saved['object_type'] == plan['object_type'], 'sync_source_changed')
            if plan['object_type'] == 'emails':
                require(saved['plan_hash'] == plan['plan_hash'], 'sync_source_changed')
            elif plan['action'] == 'existing':
                require(not state.get('returned_id') or state['returned_id'] == plan['record_id'],
                        'contact_identity_changed')
            return deepcopy(saved)
        return plan

    @staticmethod
    def _seal(plan, fingerprint):
        value = {k: v for k, v in plan.items() if k != 'plan_hash'}
        value['sync_source_hash'] = fingerprint
        return {**value, 'plan_hash': payload_hash(value)}

    def _event_plan(self, event, lead, contact_id):
        require(isinstance(event, Mapping), 'invalid_email_event')
        kind = event.get('event_type')
        require(kind in {'email_sent', 'reply_received'}, 'nonhuman_or_unverified_reply_held')
        require(event.get('content_complete') is True, 'incomplete_event_content')
        source = event.get('source', {})
        require(isinstance(source, Mapping), 'source_identity_mismatch')
        provider = event.get('provider', source.get('provider', 'apollo'))
        require(provider in {'apollo', 'gmail'}, 'source_identity_mismatch')
        # Apollo reply signals are not the original inbound message.
        require(kind != 'reply_received' or provider == 'gmail', 'original_reply_required')
        if provider == 'gmail':
            require(event.get('account_id', source.get('account_id')) == MAILBOX, 'source_identity_mismatch')
            sender = event.get('sender_email', event.get('sender'))
            if isinstance(sender, Mapping):
                sender = sender.get('email')
            require(email(sender) in contact_emails(lead)[1], 'reply_sender_mismatch')
            require(event.get('provider_message_id') == event.get('provider_id')
                    and identifier(event.get('provider_thread_id')), 'original_reply_required')
        require(event.get('account_id', source.get('account_id', APOLLO_TEAM)) == (MAILBOX if provider == 'gmail' else APOLLO_TEAM),
                'source_identity_mismatch')
        require(event.get('external_campaign_id', APOLLO_SEQUENCE) == APOLLO_SEQUENCE, 'source_identity_mismatch')
        require(event.get('source_association', 'canonical_contact') == 'canonical_contact', 'event_contact_unverified')
        require(event.get('receipt') is not False and event.get('unsent') is not True, 'event_receipt_unverified')
        if kind == 'email_sent':
            require(event.get('provider_status', 'completed') == 'completed'
                    and event.get('status') not in {'scheduled', 'draft', 'pending', 'queued', 'sending'},
                    'not_actual_sent')
        primary, aliases = contact_emails(lead)
        address = email(event.get('lead_email') or primary)
        require(address in aliases, 'recipient_identity_mismatch')
        normalized = {**deepcopy(event), 'lead_email': address,
                      'source': {'provider': provider, 'account_id': MAILBOX if provider == 'gmail' else APOLLO_TEAM,
                          'external_campaign_id': APOLLO_SEQUENCE, 'campaign_id': CAMPAIGN_ID,
                          'provider_id': event.get('provider_id')}}
        if source:
            require(dict(source) == normalized['source'], 'source_identity_mismatch')
        plan = plan_email(normalized, contact_id, source_validator=sync_source)
        plan['sync_provenance'] = {k: deepcopy(event.get(k)) for k in
            ('provider_message_id', 'provider_thread_id', 'sender', 'sender_email', 'source_association')}
        return self._seal(plan, payload_hash({'contact': lead['id'], 'event': event}))

    def _save_binding(self, key, source, contact_id, plan):
        value = {'source_contact_id': source['id'], 'source': source,
                 'account_id': APOLLO_TEAM, 'external_campaign_id': APOLLO_SEQUENCE, 'campaign_id': CAMPAIGN_ID,
                 'source_hash': payload_hash(source), 'hubspot_contact_id': contact_id,
                 'portal_id': PORTAL_ID, 'contact_plan_hash': plan['plan_hash'],
                 'company_link_status': 'company_link_pending'}
        with self.repo.transaction():
            old = self.repo.get_state(BINDING_KIND, key)
            require(old is None or (old['source_hash'] == value['source_hash']
                    and old['hubspot_contact_id'] == contact_id), 'sync_binding_changed')
            if old is None:
                self.repo.append_state(BINDING_KIND, key, value)

    def run(self, snapshot, dry_run=True, max_contacts=10, exact_emails=None, cursor=None):
        """Prepare/execute a bounded batch; writes counts transport write attempts.

        Only verified_state establishes observed CRM persistence. A returned ID or
        an incremented write count alone does not establish an external effect.
        """
        if not self._run_lock.acquire(blocking=False):
            return {'status': 'blocked', 'error_code': 'sync_busy', 'dry_run': dry_run is True,
                    'writes': 0, 'rows': [], 'next_cursor': None, 'retry_write': False}
        try:
            return self._run(snapshot, dry_run, max_contacts, exact_emails, cursor)
        finally:
            self._run_lock.release()

    def _run(self, snapshot, dry_run, max_contacts, exact_emails, cursor):
        """Returns rows/contact/events, writes, readiness and an exact-snapshot cursor.

        No DB mutation in simulation or when readiness is blocked. Errors are
        sanitized codes; uncertain writes retain the importer's original journal.
        """
        self._writes = 0
        result = {'status': 'blocked', 'dry_run': dry_run is True, 'writes': 0,
                  'rows': [], 'next_cursor': None, 'retry_write': False}
        try:
            require(type(dry_run) is bool, 'invalid_dry_run')
            require(type(max_contacts) is int and 1 <= max_contacts <= 10, 'invalid_contact_limit')
            leads = self._scope(snapshot)
            digest = payload_hash(snapshot)
            if exact_emails is not None:
                require(isinstance(exact_emails, (list, tuple)) and 0 < len(exact_emails) <= 1000,
                        'invalid_email_selection')
                selected = {email(v) for v in exact_emails}
                leads = [lead for lead in leads if selected.intersection(contact_emails(lead)[1])]
            offset = 0
            if cursor is not None:
                require(isinstance(cursor, Mapping) and set(cursor) == {'snapshot_hash', 'offset', 'selection_hash'}
                        and cursor['snapshot_hash'] == digest and cursor['selection_hash'] == payload_hash(exact_emails)
                        and type(cursor['offset']) is int and 0 <= cursor['offset'] <= len(leads), 'invalid_cursor')
                offset = cursor['offset']
            batch = leads[offset:offset + max_contacts]
            if offset + len(batch) < len(leads):
                result['next_cursor'] = {'snapshot_hash': digest, 'offset': offset + len(batch),
                                         'selection_hash': payload_hash(exact_emails)}
            result['readiness'] = self.readiness()
            if not dry_run and not result['readiness']['ready']:
                result['error_code'] = 'email_write_permission_unavailable' if result['readiness']['email_write_permission'] != 'AVAILABLE' else 'email_writers_disabled'
                return result
            identifiers = sorted({address for lead in batch for address in contact_emails(lead)[1]})
            # One batch snapshot for preparation; importer still performs fresh preflight.
            snapshot_crm = self.transport.contact_snapshot(identifiers) if identifiers else None
            importer = ExternalEmailImport(self.repo, _GuardedTransport(self), enabled=True,
                                           authorize=self._authorize, plan_validator=validate_sync_plan)
            for lead in batch:
                row = {'source_contact_id': lead['id'], 'contact_status': 'held', 'events': [],
                       'company_link_status': 'company_link_pending'}
                result['rows'].append(row)
                try:
                    source = self._contact_source(lead)
                    fingerprint = payload_hash(source)
                    key = self._binding_key(lead)
                    binding = self.repo.get_state(BINDING_KIND, key)
                    require(binding is None or binding['source_hash'] == fingerprint, 'sync_source_changed')
                    plan = self._seal(plan_contact(lead, snapshot_crm), fingerprint)
                    current_action, current_id = plan['action'], plan.get('record_id')
                    plan = self._original(plan, fingerprint)
                    row['contact_plan'] = plan
                    if plan['action'] == 'no_email':
                        row['contact_status'] = 'no_email'
                        continue
                    events = lead.get('events', [])
                    require(isinstance(events, list) and len(events) <= MAX_EVENTS_PER_CONTACT, 'event_limit')
                    if dry_run:
                        effect, _ = ExternalEmailImport._keys(plan)
                        attempt = self.repo.get_state('hubspot_attempt', effect)
                        if attempt and attempt['status'] != 'verified_state':
                            row['contact_status'] = 'needs_human' if attempt['status'] == 'needs_human' else 'pending_reconciliation'
                        else:
                            row['contact_status'] = 'would_create' if current_action == 'create' else 'existing'
                        contact_id = (binding or {}).get('hubspot_contact_id') or current_id
                    else:
                        outcome = importer.execute(plan)
                        row['contact_status'] = outcome['status']
                        if outcome['status'] != 'verified_state':
                            continue
                        contact_id = outcome['record_id']
                        self._save_binding(key, source, contact_id, plan)
                    row['hubspot_contact_id'] = contact_id
                    for event in events:
                        item = {'provider_id': event.get('provider_id') if isinstance(event, Mapping) else None,
                                'status': 'held'}
                        row['events'].append(item)
                        try:
                            # Validate facts even when a new contact has no real CRM ID yet.
                            email_plan = self._event_plan(event, lead, contact_id or '1')
                            if contact_id:
                                email_plan = self._original(email_plan, email_plan['sync_source_hash'])
                            if dry_run:
                                effect, _ = ExternalEmailImport._keys(email_plan)
                                attempt = self.repo.get_state('hubspot_attempt', effect) if contact_id else None
                                if attempt and attempt['status'] != 'verified_state':
                                    item['status'] = 'needs_human' if attempt['status'] == 'needs_human' else 'pending_reconciliation'
                                elif contact_id:
                                    dedupe = _GuardedTransport(self).email_dedupe(email_plan)
                                    require(dedupe.get('complete') is True and dedupe.get('unattributed_candidates') == 0
                                            and isinstance(dedupe.get('records'), list) and len(dedupe['records']) <= 1,
                                            'incomplete_email_dedupe')
                                    item['status'] = 'existing' if dedupe['records'] else 'would_log'
                                else:
                                    item['status'] = 'awaiting_contact_readback'
                            else:
                                item.update(importer.execute(email_plan))
                        except ExternalEmailError as exc:
                            item['error_code'] = exc.code
                        except Exception:
                            item['error_code'] = 'sync_read_unavailable'
                except ExternalEmailError as exc:
                    row['error_code'] = exc.code
                except Exception:
                    row['error_code'] = 'sync_read_unavailable'
            result['status'] = 'dry_run' if dry_run else 'processed'
            if any(row.get('error_code') or row['contact_status'] in {'held', 'pending_reconciliation', 'needs_human'}
                   or any(e.get('error_code') or e['status'] in {'held', 'pending_reconciliation', 'needs_human'} for e in row['events'])
                   for row in result['rows']):
                result['status'] = 'partial'
        except ExternalEmailError as exc:
            result['error_code'] = exc.code
        except Exception:
            result['error_code'] = 'sync_read_unavailable'
        result['writes'] = self._writes
        return result
