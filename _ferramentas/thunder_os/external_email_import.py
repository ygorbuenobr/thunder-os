"""Prepare external sent-email logs; reuse Repository's single-attempt state.

No email-send API, schema migration or implicit credential discovery is provided.
Source payloads and plans belong in private runtime storage, never source control.
"""
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import re
from urllib.parse import urlencode

from .hubspot_executor import _https_request
from .integration_hubspot import PORTAL_ID
from .models import payload_hash

APOLLO_TEAM = '6a18b5e2bba6e300203fd691'
APOLLO_SEQUENCE = '6ad7c2af749725001465b81d'
INSTANTLY_WORKSPACE = '2ead5b1b-02e1-45d7-b2f7-76e686418250'
INSTANTLY_CAMPAIGN = 'ed7ebc41-c816-40d8-832c-f7200a911c0c'
SOURCES = {'apollo': (APOLLO_TEAM, APOLLO_SEQUENCE, 'BR-APOLLO-01'),
           'instantly': (INSTANTLY_WORKSPACE, INSTANTLY_CAMPAIGN, 'BR-INSTANTLY-01')}
CONTACT_PROPERTIES = ('email', 'hs_additional_emails')
EMAIL_PROPERTIES = ('hs_timestamp', 'hs_email_direction', 'hs_email_status',
                    'hs_email_subject', 'hs_email_text')


class ExternalEmailError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise ExternalEmailError(code)


def identifier(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value))


def record_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[1-9][0-9]{0,29}', value))


def email(value):
    require(isinstance(value, str), 'invalid_email')
    value = value.strip().lower()
    require(len(value) <= 254 and bool(re.fullmatch(r'[^\s@;,<>]+@[^\s@;,<>]+\.[^\s@;,<>]+', value)),
            'invalid_email')
    return value


def timestamp(value):
    require(isinstance(value, str) and len(value) <= 40, 'invalid_timestamp')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(result.tzinfo is not None, 'invalid_timestamp')
        return result.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    except (ValueError, OverflowError):
        raise ExternalEmailError('invalid_timestamp') from None


def source_identity(value):
    require(isinstance(value, Mapping), 'invalid_source')
    pinned = SOURCES.get(value.get('provider'))
    require(pinned is not None and
            (value.get('account_id'), value.get('external_campaign_id'), value.get('campaign_id')) == pinned,
            'source_identity_mismatch')
    require(identifier(value.get('provider_id')), 'invalid_provider_id')
    return {key: value[key] for key in
            ('provider', 'account_id', 'external_campaign_id', 'campaign_id', 'provider_id')}


def contact_emails(contact):
    require(isinstance(contact, Mapping) and identifier(contact.get('id')), 'invalid_contact')
    primary = email(contact['email']) if contact.get('email') else None
    candidates = contact.get('contact_emails', [])
    require(isinstance(candidates, list), 'invalid_contact_emails')
    values = {primary} if primary else set()
    for row in candidates:
        require(isinstance(row, Mapping), 'invalid_contact_emails')
        if row.get('email'):
            values.add(email(row['email']))
    return primary, sorted(values)


def _aliases(row):
    require(isinstance(row, Mapping) and record_id(row.get('id'))
            and row.get('archived') is False, 'invalid_canonical_contact')
    props = row.get('properties')
    require(isinstance(props, Mapping) and set(CONTACT_PROPERTIES) <= props.keys(),
            'canonical_aliases_missing')
    extra = props['hs_additional_emails']
    require(extra is None or isinstance(extra, str), 'invalid_canonical_aliases')
    return {email(value) for value in [props['email'], *(extra or '').split(';')] if value}


def _seal(value):
    return {**value, 'plan_hash': payload_hash(value)}


def plan_contact(contact, snapshot):
    """Absence is valid only after complete primary AND additional-email search."""
    primary, emails = contact_emails(contact)
    base = {'portal_id': PORTAL_ID, 'object_type': 'contacts',
            'source_contact_id': contact['id'], 'identifiers': emails,
            'record_id': None, 'properties': {}, 'action': 'no_email'}
    if not primary:
        return _seal(base)
    require(isinstance(snapshot, Mapping) and str(snapshot.get('portal_id')) == PORTAL_ID
            and snapshot.get('complete') is True and snapshot.get('aliases_complete') is True
            and set(emails) <= set(snapshot.get('checked_emails', [])), 'incomplete_contact_dedupe')
    rows = snapshot.get('records')
    require(isinstance(rows, list), 'incomplete_contact_dedupe')
    matches = {row['id'] for row in rows if set(emails) & _aliases(row)}
    require(len(matches) <= 1, 'ambiguous_contact_identity')
    if matches:
        return _seal({**base, 'action': 'existing', 'record_id': next(iter(matches))})
    properties = {'email': primary}
    for key in ('first_name', 'last_name'):
        value = contact.get(key)
        if value:
            require(isinstance(value, str) and len(value) <= 200, 'invalid_contact_name')
            properties[key.replace('_', '')] = value
    return _seal({**base, 'action': 'create', 'properties': properties})


def plan_email(event, contact_id):
    """The caller must obtain event from a provider read, not a webhook alone."""
    require(isinstance(event, Mapping), 'invalid_email_event')
    source = source_identity(event.get('source'))
    require(record_id(contact_id), 'invalid_contact_id')
    kind = event.get('event_type')
    require(kind in ('email_sent', 'reply_received', 'auto_reply_received'), 'invalid_event_type')
    require(kind == 'email_sent' or (kind == 'reply_received' and event.get('human_reply') is True),
            'nonhuman_or_unverified_reply_held')
    lead = email(event.get('lead_email'))
    subject, body = event.get('subject'), event.get('text')
    require(isinstance(subject, str) and len(subject) <= 1000
            and isinstance(body, str) and len(body) <= 200_000, 'invalid_email_content')
    # This visible source reference is not an RFC header or an email message-id.
    reference = ':'.join((source['provider'], source['account_id'], source['provider_id']))
    properties = {'hs_timestamp': timestamp(event.get('occurred_at')),
                  'hs_email_direction': 'EMAIL' if kind == 'email_sent' else 'INCOMING_EMAIL',
                  'hs_email_status': 'SENT', 'hs_email_subject': subject,
                  'hs_email_text': body + '\n\n[Thunder OS source: ' + reference + ']'}
    return _seal({'portal_id': PORTAL_ID, 'object_type': 'emails', 'action': 'create',
                  'source': source, 'event_type': kind, 'human_reply': event.get('human_reply'), 'lead_email': lead,
                  'record_id': None, 'contact_id': contact_id, 'properties': properties,
                  'effect_id': reference})


def _validate_plan(plan):
    require(isinstance(plan, Mapping), 'invalid_plan')
    value = {key: item for key, item in plan.items() if key != 'plan_hash'}
    require(plan.get('plan_hash') == payload_hash(value) and plan.get('portal_id') == PORTAL_ID,
            'invalid_plan_hash')
    kind = plan.get('object_type')
    require(kind in ('contacts', 'emails'), 'invalid_object_type')
    if kind == 'contacts':
        require(plan.get('action') in ('existing', 'create', 'no_email'), 'invalid_action')
        props = plan.get('properties', {})
        require(isinstance(props, Mapping) and set(props) <= {'email', 'firstname', 'lastname'},
                'contact_overwrite_blocked')
        if plan['action'] == 'create':
            require(email(props.get('email')) in plan['identifiers'], 'invalid_contact_identity')
        if plan['action'] == 'existing':
            require(record_id(plan.get('record_id')) and not props, 'contact_overwrite_blocked')
    else:
        source_identity(plan.get('source'))
        require(record_id(plan.get('contact_id')) and set(plan.get('properties', {})) == set(EMAIL_PROPERTIES),
                'invalid_email_plan')
        require(plan.get('event_type') == 'email_sent' or
                (plan.get('event_type') == 'reply_received' and plan.get('human_reply') is True),
                'nonhuman_or_unverified_reply_held')
        require(plan['properties']['hs_email_direction'] ==
                ('EMAIL' if plan['event_type'] == 'email_sent' else 'INCOMING_EMAIL'),
                'invalid_email_direction')


class HubSpotExternalEmail:
    """Fixed origin. Canonical reads plus two opt-in CRM log/create operations."""
    def __init__(self, credential_loader=None, *, request=None, enabled=False, authorize=None, timeout=15):
        require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 30,
                'invalid_timeout')
        self._loader, self._request = credential_loader, request or _https_request
        self.enabled, self.authorize, self.timeout = enabled, authorize, timeout

    def _call(self, method, path, body=None):
        require(callable(self._loader), 'configuration_pending')
        try:
            token = self._loader()
            require(isinstance(token, str) and token and not any(c.isspace() for c in token),
                    'credential_unavailable')
            status, raw = self._request(method, path, body,
                {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, self.timeout)
            require(type(status) is int and 200 <= status < 300, 'hubspot_http_error')
            require(isinstance(raw, bytes) and len(raw) <= 1_000_000, 'invalid_response')
            data = json.loads(raw)
            require(isinstance(data, dict), 'invalid_response')
            return data
        except ExternalEmailError:
            raise
        except Exception:
            raise ExternalEmailError('hubspot_request_uncertain') from None

    def portal(self):
        require(str(self._call('GET', '/account-info/v3/details').get('portalId')) == PORTAL_ID,
                'portal_mismatch')
        return PORTAL_ID

    def _search(self, filters):
        after, seen, rows, total = None, set(), {}, None
        for _ in range(50):
            body = {'filterGroups': [{'filters': filters}],
                    'properties': list(CONTACT_PROPERTIES), 'limit': 200}
            if after is not None:
                body['after'] = after
            result = self._call('POST', '/crm/v3/objects/contacts/search', body)
            current_total = result.get('total')
            require(type(current_total) is int and 0 <= current_total <= 10000,
                    'incomplete_contact_dedupe')
            if total is None:
                total = current_total
            require(total == current_total and isinstance(result.get('results'), list),
                    'incomplete_contact_dedupe')
            for row in result['results']:
                _aliases(row)
                require(row['id'] not in rows, 'incomplete_contact_dedupe')
                rows[row['id']] = row
            cursor = result.get('paging', {}).get('next', {}).get('after')
            if cursor is None:
                require(len(rows) == total, 'incomplete_contact_dedupe')
                return list(rows.values())
            require(isinstance(cursor, (str, int)) and str(cursor) not in seen,
                    'incomplete_contact_dedupe')
            seen.add(str(cursor))
            after = str(cursor)
        raise ExternalEmailError('incomplete_contact_dedupe')

    def contact_snapshot(self, identifiers):
        values = sorted({email(value) for value in identifiers})
        require(len(values) <= 1000, 'contact_batch_too_large')
        self.portal()
        schema = self._call('GET', '/crm/v3/properties/contacts')
        require(set(CONTACT_PROPERTIES) <= {row.get('name') for row in schema.get('results', [])},
                'canonical_aliases_missing')
        rows = {}
        for start in range(0, len(values), 40):
            for row in self._search([{'propertyName': 'email', 'operator': 'IN',
                                      'values': values[start:start + 40]}]):
                rows[row['id']] = row
        if values:
            for row in self._search([{'propertyName': 'hs_additional_emails', 'operator': 'HAS_PROPERTY'}]):
                rows[row['id']] = row
        # REST alias records are inspected locally; MCP omission cannot prove absence.
        matches = [row for row in rows.values() if set(values) & _aliases(row)]
        return {'portal_id': PORTAL_ID, 'complete': True, 'aliases_complete': True,
                'checked_emails': values, 'records': matches}

    def read_object(self, kind, ident, properties):
        require(kind in ('contacts', 'emails') and record_id(ident), 'invalid_target')
        allowed = set(EMAIL_PROPERTIES) if kind == 'emails' else set(CONTACT_PROPERTIES) | {'firstname', 'lastname'}
        require(set(properties) <= allowed, 'invalid_properties')
        query = {'properties': ','.join(sorted(properties))}
        if kind == 'emails':
            query['associations'] = 'contacts'
        return self._call('GET', '/crm/v3/objects/' + kind + '/' + ident + '?' + urlencode(query))

    def email_dedupe(self, plan):
        """Bounded canonical list, including legacy logs with no provider marker.

        A possible legacy duplicate cannot be attributed to this provider ID;
        retain it for human reconciliation rather than logging a second EMAIL.
        """
        _validate_plan(plan)
        require(plan['object_type'] == 'emails', 'invalid_object_type')
        cursor, seen, records, unknown = None, set(), [], 0
        suffix = '\n\n[Thunder OS source: ' + plan['effect_id'] + ']'
        for _ in range(50):
            query = {'limit': 100, 'properties': ','.join(EMAIL_PROPERTIES), 'associations': 'contacts'}
            if cursor is not None:
                query['after'] = cursor
            data = self._call('GET', '/crm/v3/objects/emails?' + urlencode(query))
            require(isinstance(data.get('results'), list), 'incomplete_email_dedupe')
            for row in data['results']:
                require(isinstance(row, Mapping) and record_id(row.get('id'))
                        and row['id'] not in seen and row.get('archived') is False,
                        'incomplete_email_dedupe')
                seen.add(row['id'])
                props = row.get('properties', {})
                text = props.get('hs_email_text')
                if isinstance(text, str) and text.endswith(suffix):
                    records.append(row)
                elif (props.get('hs_email_subject') == plan['properties']['hs_email_subject']
                      and props.get('hs_email_direction') == plan['properties']['hs_email_direction']
                      and timestamp(props.get('hs_timestamp')) == plan['properties']['hs_timestamp']):
                    # Read associations separately; list projections can omit them.
                    full = self.read_object('emails', row['id'], EMAIL_PROPERTIES)
                    assoc = full.get('associations', {}).get('contacts', {})
                    require(not assoc.get('paging'), 'incomplete_email_dedupe')
                    if plan['contact_id'] in {item.get('id') for item in assoc.get('results', [])}:
                        unknown += 1
            next_cursor = data.get('paging', {}).get('next', {}).get('after')
            if next_cursor is None:
                return {'complete': True, 'records': records, 'unattributed_candidates': unknown}
            require(isinstance(next_cursor, (str, int)) and str(next_cursor) != cursor,
                    'incomplete_email_dedupe')
            cursor = str(next_cursor)
        raise ExternalEmailError('incomplete_email_dedupe')

    def write(self, plan):
        _validate_plan(plan)
        require(self.enabled is True, 'email_writers_disabled')
        require(callable(self.authorize) and self.authorize(deepcopy(plan)) is True,
                'email_write_not_authorized')
        require(plan['action'] == 'create', 'invalid_write')
        body = {'properties': deepcopy(plan['properties'])}
        if plan['object_type'] == 'emails':
            body['associations'] = [{'to': {'id': plan['contact_id']}, 'types': [
                {'associationCategory': 'HUBSPOT_DEFINED', 'associationTypeId': 198}]}]
        return self._call('POST', '/crm/v3/objects/' + plan['object_type'], body)


class ExternalEmailImport:
    """One persisted attempt per identity, even across restart and timeout.

    The same callback authorizes the existing human click/delegated operation.
    It is not a new approval type. Readback does not establish causation.
    """
    def __init__(self, repository, transport, *, enabled=False, authorize=None):
        self.repo, self.transport = repository, transport
        self.enabled, self.authorize = enabled, authorize

    def _authority(self, plan):
        require(self.enabled is True, 'email_writers_disabled')
        require(callable(self.authorize) and self.authorize(deepcopy(plan)) is True,
                'email_write_not_authorized')

    @staticmethod
    def _keys(plan):
        if plan['object_type'] == 'emails':
            source = plan['source']
            identity = {'provider': source['provider'], 'account': source['account_id'],
                        'message': source['provider_id']}
            keys = [payload_hash({'portal': PORTAL_ID, 'external_email': identity})]
        else:
            keys = [payload_hash({'portal': PORTAL_ID, 'type': 'contacts',
                                  'identity': {'property': 'email', 'value': value}})
                    for value in plan['identifiers']]
        effect = payload_hash({'portal': PORTAL_ID, 'external_import':
                               plan.get('effect_id', {'contact_emails': plan['identifiers']}
                                        if plan['object_type'] == 'contacts' else None)})
        return effect, keys

    def execute(self, plan):
        _validate_plan(plan)
        plan = deepcopy(plan)
        self._authority(plan)  # OFF before Repository, credentials and provider access.
        require(getattr(self.repo._local, 'connection', None) is None,
                'outer_transaction_blocked')
        if plan['action'] == 'no_email':
            return {'status': 'no_email', 'retry_write': False}
        self.transport.portal()
        effect, keys = self._keys(plan)
        with self.repo.transaction():
            state = self.repo.get_state('hubspot_attempt', effect)
            if state:
                require(state['plan_hash'] == plan['plan_hash'], 'idempotency_payload_mismatch')
            else:
                for key in keys:
                    require(self.repo.get_state('hubspot_identity', key) is None,
                            'identity_has_pending_attempt')
                self.repo.append_state('hubspot_attempt', effect, {
                    'plan_hash': plan['plan_hash'], 'plan': plan, 'status': 'reserved', 'returned_id': None})
                for key in keys:
                    self.repo.put_state('hubspot_identity', key, {'effect': effect, 'status': 'reserved'})
        if state:
            return self.reconcile(plan)
        existing_email = None
        try:
            if plan['object_type'] == 'contacts':
                fresh = self.transport.contact_snapshot(plan['identifiers'])
                source = {'id': plan['source_contact_id'], 'email':
                          plan['properties'].get('email') or plan['identifiers'][0],
                          'contact_emails': [{'email': value} for value in plan['identifiers']]}
                check = plan_contact(source, fresh)
                require(check['action'] == plan['action'] and check['record_id'] == plan['record_id'],
                        'contact_identity_changed')
            else:
                contact = self.transport.read_object('contacts', plan['contact_id'], CONTACT_PROPERTIES)
                require(plan['lead_email'] in _aliases(contact), 'contact_identity_changed')
                dedupe = self.transport.email_dedupe(plan)
                require(isinstance(dedupe, Mapping) and dedupe.get('complete') is True
                        and dedupe.get('unattributed_candidates') == 0
                        and isinstance(dedupe.get('records'), list) and len(dedupe['records']) <= 1,
                        'incomplete_email_dedupe')
                if dedupe['records']:
                    existing_email = dedupe['records'][0]['id']
                    require(record_id(existing_email), 'invalid_email_receipt')
            self._authority(plan)
        except Exception:
            self._state(effect, status='needs_human', error_code='preflight_failed')
            return {'status': 'needs_human', 'error_code': 'preflight_failed', 'retry_write': False}
        if plan['action'] == 'existing' or existing_email:
            self._state(effect, status='no_write', returned_id=existing_email or plan['record_id'])
        else:
            self._state(effect, status='attempted')
            try:
                response = self.transport.write(plan)
                require(isinstance(response, Mapping) and record_id(response.get('id')), 'invalid_write_receipt')
                self._state(effect, status='response_received', returned_id=response['id'])
            except Exception:
                return {'status': 'pending_reconciliation', 'error_code': 'email_write_uncertain',
                        'retry_write': False, 'email_logged': False}
        return self.reconcile(plan)

    def _state(self, effect, **updates):
        self.repo.put_state('hubspot_attempt', effect,
                            {**self.repo.get_state('hubspot_attempt', effect), **updates})

    def reconcile(self, plan, *, recovered_id=None):
        _validate_plan(plan)
        self._authority(plan)
        self.transport.portal()
        effect, _ = self._keys(plan)
        state = self.repo.get_state('hubspot_attempt', effect)
        require(state is not None and state['plan_hash'] == plan['plan_hash'], 'attempt_not_found')
        if state['status'] == 'needs_human':
            return {'status': 'needs_human', 'retry_write': False, 'email_logged': False}
        ident = state.get('returned_id') or recovered_id
        if not record_id(ident):
            return {'status': 'pending_reconciliation', 'retry_write': False, 'email_logged': False}
        try:
            kind = plan['object_type']
            properties = EMAIL_PROPERTIES if kind == 'emails' else set(CONTACT_PROPERTIES) | set(plan['properties'])
            row = self.transport.read_object(kind, ident, properties)
            require(isinstance(row, Mapping) and row.get('id') == ident
                    and row.get('archived') is False, 'object_readback_mismatch')
            if kind == 'contacts':
                require(set(plan['identifiers']) & _aliases(row), 'contact_identity_changed')
                if plan['action'] == 'create':
                    require(all(row['properties'].get(key) == value for key, value in plan['properties'].items()),
                            'contact_readback_mismatch')
            else:
                props = row.get('properties', {})
                for name, expected in plan['properties'].items():
                    actual = timestamp(props.get(name)) if name == 'hs_timestamp' else props.get(name)
                    require(actual == expected, 'email_readback_mismatch')
                association = row.get('associations', {}).get('contacts', {})
                require(not association.get('paging') and plan['contact_id'] in
                        {value.get('id') for value in association.get('results', [])},
                        'email_association_unverified')
        except Exception:
            return {'status': 'pending_reconciliation', 'retry_write': False, 'email_logged': False}
        self._state(effect, status='verified_state', returned_id=ident)
        return {'status': 'verified_state', 'record_id': ident, 'retry_write': False,
                'email_logged': kind == 'emails', 'association_verified': kind == 'emails'}
