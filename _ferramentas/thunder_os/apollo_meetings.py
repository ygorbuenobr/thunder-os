"""Critério 1: HubSpot meetings for the BR-APOLLO-01 cohort, read-only.

Rule approved by Ygor on 2026-11-03: a meeting counts for the campaign when it
is associated in HubSpot with a sequence contact (exact email) and was created
after the first Apollo email confirmed as sent to that lead. This is an
attribution by rule, shown with links to check, never a provider fact.
Canceled meetings never count. Contacts without an exact HubSpot match are
listed as an exception, not assumed meeting-free.
"""
from datetime import datetime, timezone
import logging
import re

from thunder_os.cadence_readonly import CadenceReadonlyError, _coverage, _partial

KIND = 'apollo_meetings_snapshot'
CAMPAIGN = 'BR-APOLLO-01'
INTERVAL = 1800
RULE = 'reunião no HubSpot com contato da sequência, criada depois do 1º e-mail Apollo enviado a ele'
PROPERTIES = ['hs_timestamp', 'hs_createdate', 'hs_meeting_title', 'hs_meeting_outcome']
CANCELED = {'CANCELED', 'CANCELLED'}
MEETING_URL = 'https://app.hubspot.com/contacts/51412240/record/0-47/'
_EMAIL = re.compile(r'[^@\s]+@[^@\s]+\.[^@\s]+')


def _date(value):
    if not isinstance(value, str) or 'T' not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


NO_ASSOCIATIONS = 'crm.associations.NO_ASSOCIATIONS_FOUND'


def contact_meetings(session, contact_ids, coverage):
    """V4 batch read contacts -> meetings.

    HubSpot answers a contact without meetings with a per-input error
    NO_ASSOCIATIONS_FOUND; that is a resolved empty list, not a partial read.
    Any other error, a missing input or a further page keeps the read partial.
    """
    links, resolved = {}, set()
    for start in range(0, len(contact_ids), 100):
        chunk = contact_ids[start:start + 100]
        result = session.call('POST', '/crm/v4/associations/contacts/meetings/batch/read',
                              {'inputs': [{'id': ident} for ident in chunk]})
        rows, errors = result.get('results'), result.get('errors') or []
        if not isinstance(rows, list) or not isinstance(errors, list):
            raise CadenceReadonlyError('provider_invalid_batch')
        for row in rows:
            ident = str((row.get('from') or {}).get('id')) if isinstance(row, dict) else None
            if ident not in chunk or not isinstance(row.get('to'), list):
                raise CadenceReadonlyError('provider_invalid_association')
            links[ident] = [str(link['toObjectId']) for link in row['to']
                            if isinstance(link, dict) and link.get('toObjectId') is not None]
            if (row.get('paging') or {}).get('next'):
                _partial(coverage, 'contact_meetings:page_limit')
            else:
                resolved.add(ident)
        for error in errors:
            context = error.get('context') if isinstance(error, dict) else None
            ids = (context or {}).get('fromObjectId') or (context or {}).get('ids') or []
            if error.get('subCategory') == NO_ASSOCIATIONS and isinstance(ids, list):
                for ident in map(str, ids):
                    if ident in chunk:
                        links.setdefault(ident, [])
                        resolved.add(ident)
            else:
                _partial(coverage, 'contact_meetings:batch_error')
    if resolved != set(contact_ids):
        _partial(coverage, 'contact_meetings:batch_records_missing')
    coverage['sources']['contact_meetings'] = {'complete': resolved == set(contact_ids)
        and not any(r.startswith('contact_meetings:') for r in coverage['limitations']),
        'requested': len(contact_ids), 'records': len(resolved)}
    return links


def first_sends(leads):
    """Exact lowercase email -> earliest confirmed Apollo send for that lead."""
    sends = {}
    for lead in leads or []:
        address = lead.get('email') if isinstance(lead, dict) else None
        if not isinstance(address, str) or not _EMAIL.fullmatch(address):
            continue
        times = [_date(e.get('occurred_at')) for e in lead.get('events', [])
                 if isinstance(e, dict) and e.get('event_type') == 'email_sent' and e.get('receipt') is True]
        times = [t for t in times if t is not None]
        if times:
            address = address.lower()
            sends[address] = min([*times, *([sends[address]] if address in sends else [])])
    return sends


def qualify(sends, contacts_by_email, meetings_by_contact, meetings):
    """Pure rule application; returns (leads_with_meeting, rows, unparseable_count)."""
    rows, leads, unparseable = [], set(), 0
    for address, first in sorted(sends.items()):
        for contact in contacts_by_email.get(address, []):
            for meeting_id in meetings_by_contact.get(contact, []):
                meeting = meetings.get(meeting_id)
                if not isinstance(meeting, dict):
                    continue
                props = meeting.get('properties') or {}
                created = _date(props.get('hs_createdate'))
                outcome = str(props.get('hs_meeting_outcome') or '').upper()
                if created is None:
                    unparseable += 1
                    continue
                if created < first or outcome in CANCELED:
                    continue
                leads.add(address)
                rows.append({'meeting_id': meeting_id, 'contact_id': contact, 'lead_email': address,
                             'created_at': props.get('hs_createdate'), 'starts_at': props.get('hs_timestamp'),
                             'title': props.get('hs_meeting_title'), 'outcome': outcome or None,
                             'first_apollo_send_at': first.isoformat(),
                             'source_url': MEETING_URL + meeting_id})
    return leads, rows, unparseable


class ApolloMeetingsReader:
    def __init__(self, repo, adapter, *, clock=None, interval=INTERVAL):
        self.repo, self.adapter = repo, adapter
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.interval = interval

    def snapshot(self):
        return self.repo.get_state(KIND, CAMPAIGN)

    def refresh(self, apollo_state):
        previous = self.snapshot() or {}
        last = _date(previous.get('last_attempt_at'))
        now = self.clock()
        if last is not None and 0 <= (now - last).total_seconds() < self.interval:
            return previous
        sends = first_sends((apollo_state or {}).get('leads'))
        if not sends:
            # No confirmed send yet: nothing can qualify, and zero would be a false total.
            return previous
        coverage = _coverage()
        state = {'campaign_id': CAMPAIGN, 'rule': RULE, 'last_attempt_at': now.isoformat(),
                 'observed_at': None, 'value': None, 'leads_with_meeting': None, 'complete': False, 'rows': [],
                 'leads_considered': len(sends), 'leads_without_crm_contact': None,
                 'leads_with_ambiguous_crm_contact': None, 'reasons': [], 'coverage': coverage}
        try:
            session = self.adapter.session()
            session.identity()
            contacts_by_email = {}
            emails = sorted(sends)
            for start in range(0, len(emails), 100):
                chunk = emails[start:start + 100]
                for row in session.pages('POST', '/crm/v3/objects/contacts/search', {
                        'filterGroups': [{'filters': [{'propertyName': 'email', 'operator': 'IN', 'values': chunk}]}],
                        'properties': ['email'], 'limit': 100}, coverage, 'meeting_contacts:%d' % (start // 100)):
                    address = str((row.get('properties') or {}).get('email') or '').lower()
                    if address in sends and row.get('archived') is not True:
                        contacts_by_email.setdefault(address, []).append(str(row['id']))
            if any(not info.get('complete') for name, info in coverage['sources'].items()
                   if name.startswith('meeting_contacts:')):
                # Unread e-mails are not "no CRM contact"; keep the previous observation.
                raise CadenceReadonlyError('contact_search_partial')
            ambiguous = {a for a, ids in contacts_by_email.items() if len(set(ids)) > 1}
            for address in ambiguous:
                contacts_by_email.pop(address)
            contact_ids = sorted({c for ids in contacts_by_email.values() for c in ids})
            associations = contact_meetings(session, contact_ids, coverage)
            if not coverage['sources'].get('contact_meetings', {}).get('complete'):
                raise CadenceReadonlyError('meeting_associations_partial')
            meeting_ids = sorted({m for ids in associations.values() for m in ids})
            meetings = {}
            for start in range(0, len(meeting_ids), 100):
                result = session.call('POST', '/crm/v3/objects/meetings/batch/read', {
                    'inputs': [{'id': m} for m in meeting_ids[start:start + 100]], 'properties': PROPERTIES})
                if not isinstance(result.get('results'), list):
                    raise CadenceReadonlyError('provider_invalid_batch')
                for row in result['results']:
                    if isinstance(row, dict) and row.get('id') is not None:
                        meetings[str(row['id'])] = row
            if set(meetings) != set(meeting_ids):
                _partial(coverage, 'meetings_batch_records_missing')
            leads, rows, unparseable = qualify(sends, contacts_by_email, associations, meetings)
            missing = len(sends) - len(contacts_by_email) - len(ambiguous)
            if unparseable:
                _partial(coverage, 'meetings_created_at_unparseable')
                state['reasons'].append('meetings_created_at_unparseable')
            state.update(value=len(rows), leads_with_meeting=len(leads), rows=rows, observed_at=self.clock().isoformat(),
                         leads_without_crm_contact=missing, leads_with_ambiguous_crm_contact=len(ambiguous),
                         complete=coverage['complete'])
            if missing:
                state['reasons'].append('meetings_contacts_without_crm_match')
            if ambiguous:
                state['reasons'].append('meetings_contacts_ambiguous_in_crm')
        except CadenceReadonlyError as exc:
            _partial(coverage, 'meetings:' + exc.code)
            state['error_code'] = exc.code
            for key in ('value', 'leads_with_meeting', 'rows', 'observed_at', 'leads_without_crm_contact',
                        'leads_with_ambiguous_crm_contact'):
                state[key] = previous.get(key, state[key])
        except Exception:
            logging.getLogger(__name__).warning('apollo_meetings_read_unavailable')
            _partial(coverage, 'meetings:read_unavailable')
            state['error_code'] = 'read_unavailable'
            for key in ('value', 'leads_with_meeting', 'rows', 'observed_at'):
                state[key] = previous.get(key, state[key])
        if coverage['limitations']:
            state['complete'] = False
            if 'meetings_crm_read_partial' not in state['reasons']:
                state['reasons'].append('meetings_crm_read_partial')
        self.repo.put_state(KIND, CAMPAIGN, state)
        return state


def metric(state, now=None, *, max_age=2 * INTERVAL + 600):
    """Panel block for by_metric['meetings']; a stale or foreign snapshot is never complete."""
    observed = _date(state.get('observed_at')) if isinstance(state, dict) else None
    if observed is None or state.get('campaign_id') != CAMPAIGN:
        return {'value': None, 'complete': False, 'reasons': ['meetings_read_in_crm'], 'rule': RULE}
    reasons = list(state.get('reasons') or [])
    age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
    fresh = 0 <= age <= max_age
    if not fresh:
        reasons.append('meetings_stale')
    return {'value': state.get('value'), 'complete': state.get('complete') is True and fresh,
            'leads_with_meeting': state.get('leads_with_meeting'),
            'reasons': reasons, 'rule': RULE,
            'observed_at': state.get('observed_at'), 'rows': list(state.get('rows') or [])[:50],
            'leads_without_crm_contact': state.get('leads_without_crm_contact'),
            'leads_with_ambiguous_crm_contact': state.get('leads_with_ambiguous_crm_contact')}
