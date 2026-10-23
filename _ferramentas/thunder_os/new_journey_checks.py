"""Bounded cold-journey checks. Positive blockers are evidence; absence is unknown.

Current research is freeform and current provider snapshots do not cover all four
membership readers. Neither can produce a clean bill. CSV fields are hints only.
Stable source references exclude consultation timestamps for review comparison.
"""
from collections import Counter
from copy import copy
from datetime import datetime, timezone
import re
import time
import unicodedata
from urllib.parse import parse_qs, urlencode, urlsplit

from .cadence_readonly import PORTAL_ID, _id
from .hubspot_executor import HubSpotHTTP
from .models import payload_hash
from .today_readonly import ReadonlyRepository, _safe_url

KEYS = ('icp', 'exclusions', 'client', 'open_deal', 'refusal', 'membership')
PROPERTIES = {'email', 'hs_additional_emails', 'company', 'lifecyclestage',
              'hs_email_optout', 'tipo_do_lead', 'campanha_ativa', 'executor'}
DEAL_PROPERTIES = {'hs_is_closed', 'dealname'}
KINDS = ('identity', 'research', 'reply', 'cadence')


class NewJourneyChecksError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def _email(value):
    return value.strip().lower() if isinstance(value, str) and len(value) <= 254 and re.fullmatch(
        r'[^\s@<>*]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', value.strip()) else None


def _token(value):
    if not isinstance(value, str):
        return None
    if _email(value):
        return _email(value)
    if value.startswith('hubspot:') and _id(value[8:]):
        return value
    try:
        return value.rstrip('/').lower() if _safe_url(value) else None
    except ValueError:
        return None


def _ref(kind, rows):
    return kind + ':' + payload_hash(rows)


class _HubSpotRead:
    def __init__(self, original):
        self.deadline, self.count, self.requests = time.monotonic() + 15, 0, 0
        self.cache, self.verified = {}, False
        self.port = HubSpotHTTP(original._loader, request=self.request, timeout=min(4, original.timeout))
        self.transport = original._request

    def request(self, method, path, body, headers, timeout):
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        allowed = method == 'GET' and path == '/account-info/v3/details'
        if method == 'POST' and path == '/crm/v3/objects/contacts/search':
            try:
                filters = body['filterGroups'][0]['filters']
                allowed = (set(body) == {'filterGroups', 'properties', 'limit'} and body['limit'] == 2
                    and len(body['filterGroups']) == 1 and len(filters) == 1
                    and set(filters[0]) == {'propertyName', 'operator', 'value'}
                    and filters[0]['propertyName'] == 'email' and filters[0]['operator'] == 'EQ'
                    and bool(_email(filters[0]['value'])) and set(body['properties']) == PROPERTIES)
            except (KeyError, TypeError, IndexError):
                allowed = False
        match = re.fullmatch(r'/crm/v3/objects/(contacts|deals)/[1-9][0-9]{0,29}', parsed.path)
        if method == 'GET' and match and not parsed.netloc and not parsed.fragment:
            wanted = PROPERTIES if match[1] == 'contacts' else DEAL_PROPERTIES
            allowed = (set(query) == ({'properties', 'associations'} if match[1] == 'contacts' else {'properties'})
                and query.get('properties') == [','.join(sorted(wanted))]
                and (match[1] != 'contacts' or query.get('associations') == ['deals']))
        remaining = self.deadline - time.monotonic()
        if not allowed or remaining <= 0 or self.requests >= 41:
            raise NewJourneyChecksError('checks_hubspot_read_blocked')
        self.requests += 1
        return self.transport(method, path, body, headers, min(timeout, 4, remaining))

    def contact(self, email, ident):
        key = email, ident
        if key in self.cache:
            return self.cache[key]
        if self.count >= 10 or time.monotonic() >= self.deadline:
            return None, [], 'checks_hubspot_budget_exhausted'
        self.count += 1
        try:
            if not self.verified:
                self.port.portal()
                self.verified = True
            if not ident:
                result = self.port._call('POST', '/crm/v3/objects/contacts/search', {
                    'filterGroups': [{'filters': [{'propertyName': 'email', 'operator': 'EQ', 'value': email}]}],
                    'properties': sorted(PROPERTIES), 'limit': 2})
                rows = result.get('results')
                if (not isinstance(rows, list) or len(rows) != 1 or result.get('total') != 1
                        or result.get('paging', {}).get('next')):
                    raise NewJourneyChecksError('checks_hubspot_identity_unresolved')
                ident = _id(rows[0].get('id'))
            if not ident:
                raise NewJourneyChecksError('checks_hubspot_identity_unresolved')
            contact = self.port._call('GET', '/crm/v3/objects/contacts/' + ident + '?' + urlencode({
                'properties': ','.join(sorted(PROPERTIES)), 'associations': 'deals'}))
            props = contact.get('properties', {})
            aliases = {_email(v) for v in [props.get('email'), *(props.get('hs_additional_emails') or '').split(';')]} - {None}
            if contact.get('id') != ident or contact.get('archived') is not False or email and email not in aliases:
                raise NewJourneyChecksError('checks_hubspot_identity_mismatch')
            deals = []
            associations = contact.get('associations', {}).get('deals', {})
            for row in associations.get('results', [])[:3]:
                did = _id(row.get('id'))
                if did:
                    value = self.port._call('GET', '/crm/v3/objects/deals/' + did + '?' + urlencode({
                        'properties': ','.join(sorted(DEAL_PROPERTIES))}))
                    if value.get('id') == did and value.get('archived') is False:
                        deals.append(value)
            answer = contact, deals, None
        except Exception as exc:
            code = exc.code if isinstance(exc, NewJourneyChecksError) else 'checks_hubspot_read_unavailable'
            answer = None, [], code
        self.cache[key] = answer
        return answer


class NewJourneyChecks:
    def __init__(self, service, hubspot=None, clock=None):
        self.repo = ReadonlyRepository(service.repository.path)
        self.service = copy(service)
        self.service.repository = self.repo
        from .campaign_registry import CampaignRegistry
        self.service.campaign_registry = CampaignRegistry(self.repo)
        self.hubspot, self.clock = hubspot, clock or (lambda: datetime.now(timezone.utc))

    def __call__(self, rows, campaign_id):
        if (not isinstance(rows, list) or len(rows) > 200 or any(not isinstance(r, dict) for r in rows)
                or not isinstance(campaign_id, str) or not re.fullmatch(r'BR-[A-Za-z0-9_-]{1,97}', campaign_id)):
            raise NewJourneyChecksError('checks_input_invalid')
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise NewJourneyChecksError('checks_clock_invalid', 503)
        now = now.astimezone(timezone.utc)
        state, unavailable = {}, []
        try:
            with self.repo.transaction():
                for kind in KINDS:
                    try:
                        state[kind] = self.repo.list_state(kind)
                    except Exception:
                        state[kind] = []
                        unavailable.append(kind)
        except Exception:
            state, unavailable = {k: [] for k in KINDS}, list(KINDS)
        try:
            sheet = self.service.campaign_sheet_view(campaign_id)
            sheet_ref = _ref('campaign_sheet', {'fingerprint': sheet.get('fingerprint'), 'status': sheet.get('status'),
                'fields': [{k: f.get(k) for k in ('key', 'status', 'expected_sha256', 'observed_sha256', 'content')}
                           for f in sheet.get('fields', [])]})
            sheet_reason = 'structured_account_icp_proof_unavailable' if sheet.get('status') == 'complete' else 'current_campaign_sheet_incomplete'
        except Exception:
            sheet_ref, sheet_reason = 'campaign_sheet:unavailable', 'current_campaign_sheet_unavailable'
        groups = []
        for identity in state['identity']:
            records = identity.get('records', [])
            if (identity.get('status') != 'canonical' or identity.get('actor') != 'ygor'
                    or not self.service._identity_records_match(records)
                    or not _token(identity.get('canonical_person'))
                    or any(_token(r.get('person_key')) != _token(identity['canonical_person'])
                           or r.get('domain') != identity.get('canonical_account') for r in records)):
                continue
            ids = {str(r['hubspot_contact_id']) for r in records if _id(r.get('hubspot_contact_id'))}
            keys = {_token(identity.get('canonical_person')), *(_token(r.get('person_key')) for r in records)} - {None}
            groups.append((keys, ids, identity))
        port = _HubSpotRead(self.hubspot) if isinstance(self.hubspot, HubSpotHTTP) else None
        duplicates = Counter(r.get('source_key') for r in rows if isinstance(r.get('source_key'), str))
        output = []
        for row in rows:
            source_key = row.get('source_key')
            if (not isinstance(source_key, str) or not 1 <= len(source_key) <= 200
                    or any(ord(c) < 32 or ord(c) == 127 for c in source_key)):
                raise NewJourneyChecksError('checks_source_key_invalid')
            email = _email(row.get('email'))
            profile = _token(row.get('linkedin_url') or row.get('linkedin'))
            hinted_id = _id(row.get('hubspot_id') or row.get('hubspot_contact_id'))
            keys = {email, profile} - {None}
            matched = [g for g in groups if keys & g[0] or not keys and hinted_id in g[1]]
            ids = set().union(*(g[1] for g in matched)) if matched else set()
            identity_reason = 'identity_unlinked'
            if len(ids) > 1 or hinted_id and ids and hinted_id not in ids or duplicates[source_key] > 1:
                identity_reason = 'identity_ambiguous'
            elif len(ids) == 1:
                identity_reason = None
                keys.add('hubspot:' + next(iter(ids)))
                for g in groups:
                    if g[1] == ids:
                        keys.update(g[0])
            identity_ref = _ref('identity', [g[2] for g in matched])
            checks = {k: {'key': k, 'status': 'unknown', 'reason': {
                'icp': sheet_reason, 'exclusions': 'exclusions_coverage_unavailable',
                'client': 'client_coverage_unavailable', 'open_deal': 'account_open_deal_coverage_unavailable',
                'refusal': 'cross_channel_reply_coverage_unavailable',
                'membership': 'four_provider_membership_coverage_unavailable'}[k],
                'source': 'checks:' + k + ':coverage_unavailable',
                'checked_at': now.isoformat(), 'coverage': {'complete': False, 'identity_verified': identity_reason is None}}
                for k in KEYS}

            def block(key, reason, source):
                checks[key].update(status='blocked', reason=reason, source=source)

            company = row.get('company') if isinstance(row.get('company'), str) else ''
            hint = company[:2000] + ' ' + (email.split('@')[1] if email else '')
            folded = ''.join(c for c in unicodedata.normalize('NFKD', hint.casefold()) if not unicodedata.combining(c))
            for key, pattern, reason in (
                ('client', r'\b(?:kawasaki|pta\s+rental)\b', 'protected_client_literal_requires_review'),
                ('client', r'\b(?:bimbo|cuetara|gonac)\b', 'loa_approval_required'),
                ('exclusions', r'\b(?:coca[ -]?cola|femsa|bebidas|beverages|cervejaria|yamaha)\b', 'fixed_exclusion_literal')):
                if re.search(pattern, folded):
                    block(key, reason, _ref('policy:fixed_exclusions_v1:untrusted_hint', folded))
            contact, deals, error = None, [], 'checks_hubspot_unavailable'
            if port and identity_reason != 'identity_ambiguous' and (email or hinted_id):
                contact, deals, error = port.contact(email, hinted_id)
            if contact:
                ident = str(contact['id'])
                if ids and ids != {ident}:
                    identity_reason = 'identity_ambiguous'
                else:
                    keys.add('hubspot:' + ident)
                    for g in groups:
                        if g[1] == {ident}:
                            keys.update(g[0])
                    identity_reason = None
                props, ref = contact['properties'], _ref('hubspot:' + PORTAL_ID + ':contact:' + ident, contact)
                if props.get('lifecyclestage') == 'customer':
                    block('client', 'hubspot_current_customer', ref)
                if props.get('hs_email_optout') in (True, 'true'):
                    block('refusal', 'hubspot_email_optout_person_requires_review', ref)
                if props.get('tipo_do_lead') in {'SPAM', 'CONCORRENTE', 'NAO E ICP'}:
                    block('icp', 'hubspot_disqualified_contact', ref)
                if isinstance(props.get('campanha_ativa'), str) and props['campanha_ativa'].strip():
                    block('membership', 'hubspot_current_campaign_membership', ref)
                elif 'campanha_ativa' not in props or 'executor' not in props:
                    checks['membership']['reason'] = 'hubspot_membership_property_missing'
                for deal in deals:
                    if deal.get('properties', {}).get('hs_is_closed') in (False, 'false'):
                        block('open_deal', 'hubspot_open_deal', _ref('hubspot:deal', deal))
            if identity_reason != 'identity_ambiguous':
                for reply in state['reply']:
                    linked = _token(reply.get('person_key')) in keys or 'hubspot:' + str(reply.get('hubspot_contact_id')) in keys
                    proven = reply.get('state') == 'classified' or reply.get('identity_confirmed') is True
                    if linked and proven:
                        if reply.get('classification') in {'negative', 'opt_out', 'risk'}:
                            block('refusal', 'canonical_refusal_all_channels', _ref('repository:reply', reply))
                        elif (reply.get('human_reply') is True or reply.get('state') == 'classified') and checks['refusal']['status'] != 'blocked':
                            block('refusal', 'human_reply_requires_conversation_review', _ref('repository:reply', reply))
                for cadence in state['cadence']:
                    if (_token(cadence.get('person_key')) in keys or 'hubspot:' + str(cadence.get('hubspot_contact_id')) in keys):
                        if cadence.get('status') == 'paused_reply' and checks['refusal']['status'] != 'blocked':
                            block('refusal', 'canonical_cadence_paused_reply', _ref('repository:cadence', cadence))
                        elif cadence.get('status') in {'ready_for_review', 'approved', 'active'}:
                            block('membership', 'existing_canonical_cadence', _ref('repository:cadence', cadence))
            if identity_reason:
                for check in checks.values():
                    if check['status'] == 'unknown':
                        check['reason'] = identity_reason
            accounts = {g[2].get('canonical_account') for g in matched} - {None}
            research = [r for r in state['research'] if r['id'] in accounts]
            if checks['icp']['status'] == 'unknown':
                checks['icp']['source'] = _ref('checks:icp', {'identity': identity_ref, 'research': research, 'sheet': sheet_ref})
                for r in research:
                    for fact in r.get('facts', []):
                        try:
                            age = (now.date() - datetime.fromisoformat(fact['observed_at']).date()).days
                            if age < 0 or age > 14:
                                checks['icp']['reason'] = 'canonical_research_stale'
                        except (ValueError, TypeError, KeyError):
                            continue
            for check in checks.values():
                check['coverage']['identity_verified'] = identity_reason is None
            output.append({'source_key': source_key, 'rowstablekey': row.get('rowstablekey') or source_key,
                'checks': list(checks.values()), 'checked_at': now.isoformat(), 'retained': True,
                'coverage': {'complete': False, 'unavailable': unavailable, 'hubspot_reason': error,
                             'membership_required': ['apollo', 'instantly', 'chattie', 'laura']}})
        return output
