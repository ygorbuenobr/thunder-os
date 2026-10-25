"""R3 contact history over existing read ports; no policy, writer or cache.

``ContactHistoryReadonly(hubspot, cadence_reader, seqd=None, repo=None,
clock=None).read(row, qualification_context)`` returns contact_id, company_id,
observed_at, events, sources, complete and read_only=True. Event fields are id,
provider, date, direction (incoming/outgoing/unknown), subject, text,
body_complete, source_url and thread_id. Source fields are provider, status,
complete and reason. IDs are provider-namespaced; dates are UTC ISO strings.

hubspot is the ORIGINAL HubSpotHTTP, cadence_reader a CadenceReadonlyConsole;
seqd accepts the existing SEQDClient or enabled SEQDConsole, repo the existing
list_state('gmail_thread') read port. The route owner enforces authentication.
Text is untrusted plain text, NOT HTML-safe: render with textContent/escaping.
CRM logs/thread context are not delivery receipts or authorization to act.

HubSpot uses the current v3 batch-read/v4 association contracts, pinned portal
and owner, pagination, timeouts and shared read admission of cadence_readonly.
Fresh eligibility is constructed per invocation. No retries are added.
Gmail uses ONLY known, hash-checked thread IDs bound to freshly verified email
aliases; the bridge has no contact search. Even complete known threads cannot
prove discovery coverage, so Gmail and overall completeness remain false.
No provider SDK, network transport, credential discovery or SQL is added here.
"""
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import getaddresses
import math
import re
import time
from urllib.parse import quote

from .cadence_readonly import CadenceReadonlyError, PORTAL_ID, _date, _id, plain_text
from .models import payload_hash
from .new_journey_hubspot import HubSpotEligibilityRead
from .new_journey_qualification import candidate
from .seqd_client import MAILBOX, SEQDClient, SEQDError


def _literal(value):
    """Plain originals retain whitespace/literal tags; redact credential forms."""
    if not isinstance(value, str):
        return ''
    value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value)
    value = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [REDACTED]', value)
    value = re.sub(r'\bpat-[A-Za-z0-9_-]+', '[REDACTED]', value)
    return re.sub(r'(?i)((?:access_token|api_key|token|password)\s*[=:]\s*)[^\s&]+',
                  r'\1[REDACTED]', value)


def _addresses(value):
    if not isinstance(value, str) or len(value) > 4096 or '\r' in value or '\n' in value:
        return set()
    return {address.lower() for _, address in getaddresses([value]) if address.count('@') == 1}


def _hex(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{1,64}', value) is not None


def _source(provider, complete=False, reason='', status=None):
    return {'provider': provider, 'status': status or ('ok' if complete else 'partial'),
            'complete': complete, 'reason': reason}


class _ActivitySession:
    """Observe the same batch read: project_activity loses email direction/text.

    Delegated pages/properties retain all current budget/pagination defenses.
    Only batch metadata is retained, request-locally; no extra fetch or writer.
    """
    def __init__(self, session):
        self.session, self.emails, self.failures = session, {}, []

    def __getattr__(self, key):
        return getattr(self.session, key)

    def call(self, method, path, body=None):
        try:
            result = self.session.call(method, path, body)
        except CadenceReadonlyError as exc:
            self.failures.append(exc.code + (':http_' + str(exc.status) if exc.status else ''))
            raise
        if method == 'POST' and path == '/crm/v3/objects/emails/batch/read':
            for row in result.get('results', []):
                if isinstance(row, dict) and _id(row.get('id')) and isinstance(row.get('properties'), dict):
                    self.emails.setdefault('emails:' + str(row['id']), row['properties'])
        return result


class ContactHistoryReadonly:
    def __init__(self, hubspot, cadence_reader, seqd=None, repo=None, clock=None):
        self.hubspot, self.cadence_reader = hubspot, cadence_reader
        self.seqd, self.repo, self.clock = seqd, repo, clock or time.time

    def _observed_at(self):
        now = self.clock()
        if type(now) in (int, float) and math.isfinite(now):
            now = datetime.fromtimestamp(now, timezone.utc)
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError('contact_history_clock_invalid')
        return now.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')

    def read(self, row, qualification_context):
        result = {'contact_id': None, 'company_id': None, 'observed_at': self._observed_at(),
                  'events': [], 'sources': [], 'complete': False, 'read_only': True}
        context = qualification_context if isinstance(qualification_context, Mapping) else {}
        valid = (isinstance(row, Mapping) and isinstance(context.get('binding_hash'), str)
                 and re.fullmatch(r'[a-f0-9]{64}', context['binding_hash']) is not None
                 and all(isinstance(context.get(key), str) and _id(context[key])
                         for key in ('contact_id', 'company_id')))
        evidence, reason, statuses = None, 'contact_history_identity_required', []
        if valid:
            profiles = context.get('verified_linkedin_urls', [])
            profile = row.get('linkedin_url') or (
                profiles[0] if isinstance(profiles, list) and len(profiles) == 1 else None)
            try:
                reader = HubSpotEligibilityRead(self.hubspot)
                transport, statuses = reader._transport, []
                def observe(method, path, body, headers, timeout):
                    response = transport(method, path, body, headers, timeout)
                    if isinstance(response, tuple) and response and type(response[0]) is int:
                        statuses.append(response[0])
                    return response
                reader._transport = observe
                evidence = reader.lookup(row.get('email'), contact_id=context['contact_id'], profile=profile)
                fresh = candidate(evidence)
                valid = (fresh is not None and fresh['binding_hash'] == context['binding_hash']
                         and fresh['contact_id'] == context['contact_id']
                         and fresh['company_id'] == context['company_id'])
                reason = 'contact_history_identity_changed' if fresh else evidence.get('reason')
                if 429 in statuses:
                    reason = 'contact_history_identity_http_429'
            except Exception:
                valid, reason = False, 'contact_history_identity_unavailable'
        if not valid:
            result['sources'] = [_source('hubspot_identity', reason=reason, status='blocked'),
                _source('hubspot', reason='contact_history_identity_required', status='blocked'),
                _source('gmail', reason='contact_history_identity_required', status='blocked')]
            return result
        result.update(contact_id=context['contact_id'], company_id=context['company_id'])
        result['sources'].append(_source('hubspot_identity', 429 not in statuses,
            'fresh_binding_verified' + (';eligibility_http_429' if 429 in statuses else '')))
        events, source = self._hubspot(context['contact_id'])
        result['events'].extend(events)
        result['sources'].append(source)
        events, source = self._gmail(evidence['identity']['emails'], context['contact_id'])
        result['events'].extend(events)
        result['sources'].append(source)
        # Deduplicate only exact provider message identity, never text or names.
        unique = {event['id']: event for event in result['events']}
        result['events'] = sorted(unique.values(), key=lambda event: (event['date'] or '~', event['id']))
        result['complete'] = all(source['complete'] for source in result['sources'])
        return result

    def _hubspot(self, contact_id):
        events, reasons = [], []
        try:
            session = _ActivitySession(self.cadence_reader.adapter.session())
            session.identity()
            observed = self.cadence_reader._activities(session, contact_id)
            coverage = observed['coverage']
            reasons.extend(coverage['limitations'])
            reasons.extend(session.failures)
            for info in coverage['sources'].values():
                if info.get('http_status') is not None:
                    reasons.append('http_' + str(info['http_status']))
            for raw in observed['events']:
                props = session.emails.get(raw['id']) if raw['type'] == 'email' else None
                direction, text, subject = 'unknown', _literal(raw.get('text')), _literal(raw.get('subject'))
                if props is not None:
                    direction = {'EMAIL': 'outgoing', 'FORWARDED_EMAIL': 'outgoing',
                                 'INCOMING_EMAIL': 'incoming'}.get(props.get('hs_email_direction'), 'unknown')
                    original = props.get('hs_email_text')
                    text = _literal(original) if isinstance(original, str) and original.strip() else plain_text(props.get('hs_email_html'))
                    subject = _literal(props.get('hs_email_subject'))
                body_complete = bool(text.strip())
                if not body_complete or not raw.get('occurred_at') or raw['type'] == 'email' and direction == 'unknown':
                    reasons.append('activity_original_or_metadata_incomplete')
                # Existing CRM objects are context logs, not external receipts.
                events.append({'id': 'hubspot:' + raw['id'], 'provider': 'hubspot',
                    'date': raw.get('occurred_at'), 'direction': direction, 'subject': subject,
                    'text': text, 'body_complete': body_complete,
                    'source_url': f'https://app.hubspot.com/contacts/{PORTAL_ID}/record/0-1/{contact_id}',
                    'thread_id': None})
            complete = coverage['complete'] is True and not reasons
            return events, _source('hubspot', complete, ';'.join(dict.fromkeys(reasons)) or 'contact_activities_observed')
        except CadenceReadonlyError as exc:
            reason = exc.code + (':http_' + str(exc.status) if exc.status else '')
        except Exception:
            reason = 'contact_history_hubspot_unavailable'
        return events, _source('hubspot', reason=reason, status='unavailable')

    def _known_threads(self, aliases, contact_id):
        known, reasons = set(), []
        if self.repo is None:
            return [], ['gmail_known_threads_unavailable']
        try:
            rows = self.repo.list_state('gmail_thread')
            if not isinstance(rows, list) or len(rows) > 10000:
                return [], ['gmail_repository_limit_or_invalid']
            for row in rows:
                if not isinstance(row, Mapping) or row.get('mailbox') != MAILBOX:
                    continue
                messages = row.get('messages')
                if (not isinstance(messages, list) or not 1 <= len(messages) <= 100
                        or not all(isinstance(message, Mapping) for message in messages)
                        or row.get('source_hash') != payload_hash(messages)):
                    reasons.append('gmail_known_thread_invalid')
                    continue
                ids = {message.get('thread_id') for message in messages if _hex(message.get('thread_id'))}
                if (len(ids) != 1 or any(not _hex(message.get('thread_id'))
                                       or message.get('thread_id') not in ids for message in messages)
                        or row.get('contact_id') not in (None, contact_id)):
                    reasons.append('gmail_known_thread_identity_unverified')
                    continue
                # A persisted inbox observation proves the exact alias, never a
                # display name. Sent-only legacy imports lack a recipient field.
                bound = any(message.get('outgoing') is False
                            and _addresses(message.get('sender')) <= aliases
                            and bool(_addresses(message.get('sender'))) for message in messages)
                if not bound:
                    reasons.append('gmail_known_thread_alias_unverified')
                    continue
                known.update(ids)
        except Exception:
            reasons.append('gmail_repository_unavailable')
        if len(known) > 10:
            reasons.append('gmail_known_thread_limit')
        return sorted(known)[:10], reasons

    def _gmail(self, aliases, contact_id):
        reasons, events = ['gmail_discovery_incomplete'], []
        if self.seqd is None:
            return events, _source('gmail', reason='gmail_not_configured;gmail_discovery_incomplete', status='unavailable')
        client = self.seqd
        if hasattr(client, 'port'):
            if client.read_enabled is not True:
                return events, _source('gmail', reason='gmail_read_disabled;gmail_discovery_incomplete', status='unavailable')
            client = client.port
        threads, limitations = self._known_threads(set(aliases), contact_id)
        reasons.extend(limitations)
        if not threads:
            reasons.append('gmail_no_verified_known_threads')
        deadline = time.monotonic() + 15
        statuses = []
        if isinstance(client, SEQDClient):
            original = client
            def bounded(method, path, body, headers, timeout):
                remaining = deadline - time.monotonic()
                if method != 'GET' or not re.fullmatch(r'/v1/threads/[a-f0-9]{1,64}', path) or remaining <= 0:
                    raise SEQDError('contact_history_read_budget')
                response = original._request(method, path, body, headers, min(4, remaining))
                if isinstance(response, tuple) and response and type(response[0]) is int:
                    statuses.append(response[0])
                return response
            client = SEQDClient(original._loader, request=bounded)
        for ident in threads:
            if time.monotonic() >= deadline:
                reasons.append('gmail_read_budget_exhausted')
                break
            try:
                response = client.conversation(ident)
                if response.get('mailbox') != MAILBOX or response.get('status') != 'ok':
                    raise ValueError()
                thread = response['thread']
                if (thread.get('id') != ident or thread.get('context_only') is not True
                        or thread.get('receipt_eligible') is not False
                        or not isinstance(thread.get('messages'), list) or not 1 <= len(thread['messages']) <= 100
                        or not isinstance(thread.get('coverage'), Mapping)):
                    raise ValueError()
                if thread.get('complete') is not True or thread.get('truncated') is not False or thread['coverage'].get('reasons'):
                    reasons.append('gmail_known_thread_partial')
                for message in thread['messages']:
                    event = self._gmail_event(message, ident, set(aliases))
                    if event is None:
                        reasons.append('gmail_message_identity_unverified')
                        continue
                    events.append(event)
                    if not event['body_complete'] or not event['date']:
                        reasons.append('gmail_message_partial')
            except Exception:
                reasons.append('gmail_thread_unavailable')
        reasons.extend('gmail_http_' + str(status) for status in statuses if status >= 400)
        status = 'unavailable' if threads and not events and 'gmail_thread_unavailable' in reasons else 'partial'
        return events, _source('gmail', reason=';'.join(dict.fromkeys(reasons)), status=status)

    @staticmethod
    def _gmail_event(message, ident, aliases):
        if (not isinstance(message, Mapping) or not _hex(message.get('id'))
                or message.get('thread_id') != ident):
            return None
        sender = _addresses(message.get('from'))
        recipients = set().union(*(_addresses(message.get(field)) for field in ('to', 'cc', 'bcc')))
        labels = message.get('labelIds')
        if not isinstance(labels, list) or 'DRAFT' in labels:
            return None
        outgoing = sender == {MAILBOX} and bool(recipients & aliases) and 'SENT' in labels
        incoming = len(sender) == 1 and bool(sender & aliases) and MAILBOX in recipients
        if not outgoing and not incoming:
            return None
        bodies = message.get('bodies')
        if not isinstance(bodies, Mapping):
            bodies = {}
        text = bodies.get('text')
        text = _literal(text) if isinstance(text, str) and text.strip() else plain_text(bodies.get('html'))
        coverage = message.get('coverage')
        complete = (message.get('complete') is True and bool(text.strip())
                    and isinstance(coverage, Mapping) and coverage.get('reasons') == [])
        return {'id': 'gmail:' + message['id'], 'provider': 'gmail',
            'date': _date(message.get('internal_date')), 'direction': 'outgoing' if outgoing else 'incoming',
            'subject': _literal(message.get('subject')), 'text': text, 'body_complete': complete,
            'source_url': 'https://mail.google.com/mail/u/' + quote(MAILBOX, safe='') + '/#all/' + ident,
            'thread_id': ident}
